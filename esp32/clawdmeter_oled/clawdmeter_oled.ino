// Clawdmeter OLED — usage Claude sur ESP32 + SSD1306 0,96" bicolore (128x64).
//
// Lit http://<pc>:8080/esp.json (clawdmeter_server.py). Tout est deja formate
// cote serveur : le firmware ne fait qu'afficher.
//
// Ecran : bande jaune (16 px) = limite de la page en cours, en gros + statut,
//         zone bleue = pages qui defilent (limites, repartition de la semaine).
//
// Compilation : arduino-cli compile --fqbn esp32:esp32:esp32 esp32/clawdmeter_oled
// Libs        : Adafruit SSD1306, Adafruit GFX, ArduinoJson (v7)

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>
#include "secrets.h"

#ifndef SERVER_URL_ALT
#define SERVER_URL_ALT SERVER_URL
#endif

#define SCREEN_W   128
#define SCREEN_H   64
#define OLED_ADDR  0x3C
#define HEADER_H   16        // bande jaune de l'ecran
#define FETCH_MS   30000     // le serveur se met a jour toutes les 60 s
#define RETRY_MS   10000
#define PAGE_MS    6000
#define STALE_S    300       // au-dela, les donnees sont signalees "ancien"
#define WIFI_RETRY_MS 20000

Adafruit_SSD1306 display(SCREEN_W, SCREEN_H, &Wire, -1);

// Paires SDA/SCL connues des cartes ESP32 + OLED (Ideaspark standard en premier)
static const int OLED_PINS[][2] = {{21, 22}, {4, 15}, {5, 4}, {14, 12}, {22, 21}};

// Le PC est joignable sous deux adresses selon le sous-reseau du WiFi
static const char *SERVER_URLS[] = {SERVER_URL, SERVER_URL_ALT};
int urlIndex = 0;

struct Limit { char n[8]; int p; char r[8]; int s; bool a; };
struct Share { char n[8]; int p; };

Limit limits[6];
int nLimits = 0;
Share shares[6];
int nShares = 0;
bool hasSpend = false, spendOn = false;
char spendUsed[12] = "", spendMax[10] = "", spendCur[5] = "";

char plan[12] = "";
char apiMsg[24] = "";
bool apiOk = false, haveData = false, serverOk = false, wifiLogged = false;
long ageAtFetch = 0;
unsigned long fetchedAt = 0, nextFetchAt = 0, wifiLostAt = 0;
bool oledOk = false;
unsigned long lastOledTry = 0;
int page = 0;
int focusLimit = -1;      // limite mise en avant par la page affichee
unsigned long pageAt = 0;

bool configured() { return strcmp(WIFI_SSID, "TON_WIFI") != 0; }

// ---------------------------------------------------------------------------
// Ecran
// ---------------------------------------------------------------------------

// Apres un reset en pleine transaction, l'ecran peut tenir SDA bas et bloquer
// le bus : on impulse SCL jusqu'a ce qu'il relache la ligne.
void i2cRecover(int sda, int scl) {
  pinMode(sda, INPUT_PULLUP);
  pinMode(scl, OUTPUT);
  for (int i = 0; i < 9 && digitalRead(sda) == LOW; i++) {
    digitalWrite(scl, LOW);
    delayMicroseconds(5);
    digitalWrite(scl, HIGH);
    delayMicroseconds(5);
  }
  pinMode(scl, INPUT_PULLUP);
}

bool initOled() {
  for (const auto &pins : OLED_PINS) {
    Wire.end();
    i2cRecover(pins[0], pins[1]);
    Wire.begin(pins[0], pins[1]);
    delay(50);
    Wire.beginTransmission(OLED_ADDR);
    if (Wire.endTransmission() == 0) {
      Serial.printf("OLED trouve : SDA=%d SCL=%d\n", pins[0], pins[1]);
      // periphBegin=false : ne pas reinitialiser Wire sur les broches par defaut
      return display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR, false, false);
    }
  }
  Serial.println("OLED introuvable");
  return false;
}

void textAt(int x, int y, const char *s, int size = 1) {
  display.setTextSize(size);
  display.setCursor(x, y);
  display.print(s);
}

void textRight(int xRight, int y, const char *s, int size = 1) {
  textAt(xRight - (int)strlen(s) * 6 * size, y, s, size);
}

void drawBar(int x, int y, int w, int h, int p, bool hideFill) {
  display.drawRect(x, y, w, h, SSD1306_WHITE);
  if (hideFill) return;
  int fill = (w - 2) * constrain(p, 0, 100) / 100;
  display.fillRect(x + 1, y + 1, fill, h - 2, SSD1306_WHITE);
}

// Motifs pour distinguer les segments sur un ecran monochrome
void fillPattern(int x, int y, int w, int h, int pattern) {
  for (int i = 0; i < w; i++) {
    for (int j = 0; j < h; j++) {
      int px = x + i, py = y + j;
      bool on;
      switch (pattern % 4) {
        case 0:  on = true; break;
        case 1:  on = (px + py) % 2 == 0; break;
        case 2:  on = px % 3 == 0; break;
        default: on = (px % 2 == 0) && (py % 2 == 0); break;
      }
      if (on) display.drawPixel(px, py, SSD1306_WHITE);
    }
  }
}

long dataAge() {
  return ageAtFetch + (long)((millis() - fetchedAt) / 1000);
}

const char *statusText() {
  if (WiFi.status() != WL_CONNECTED) return "WiFi X";
  if (!serverOk) return "Serveur X";
  if (!apiOk) return "API X";
  if (dataAge() > STALE_S) return "ancien";
  return plan[0] ? plan : "ok";
}

const Limit *primaryLimit() {
  for (int i = 0; i < nLimits; i++) {
    if (limits[i].a) return &limits[i];
  }
  return nLimits ? &limits[0] : nullptr;
}

// Limite affichee en gros : celle de la page en cours, sinon l'active
const Limit *headerLimit() {
  if (focusLimit >= 0 && focusLimit < nLimits) return &limits[focusLimit];
  return primaryLimit();
}

void drawHeader() {
  const Limit *l = headerLimit();
  if (!haveData || !l) {
    textAt(0, 0, "CLAUDEMETER");
    textAt(0, 8, statusText());
    return;
  }
  char big[6];
  snprintf(big, sizeof big, "%d%%", l->p);
  textAt(0, 1, big, 2);
  char line[20];
  snprintf(line, sizeof line, "%s %s", l->n, l->r);
  textRight(SCREEN_W, 0, line);
  textRight(SCREEN_W, 8, statusText());
}

// 3 limites a l'ecran : libelle inverse pour celle de la page en cours,
// barre, % (suivi de * pour la limite la plus contraignante), reset
void drawLimits(int focusIdx) {
  bool blinkOff = (millis() / 500) % 2;
  int from = 0;
  if (nLimits > 3) {
    from = constrain(focusIdx - 1, 0, nLimits - 3);
  }
  for (int i = 0; i < 3 && from + i < nLimits; i++) {
    int idx = from + i;
    const Limit &l = limits[idx];
    int y = HEADER_H + i * 16;
    if (idx == focusIdx) {
      display.fillRect(0, y, 32, 8, SSD1306_WHITE);
      display.setTextColor(SSD1306_BLACK);
    }
    textAt(1, y, l.n);
    display.setTextColor(SSD1306_WHITE);

    drawBar(34, y + 1, 66, 6, l.p, l.s >= 2 && blinkOff);
    char pct[7];
    snprintf(pct, sizeof pct, "%d%%%s", l.p, l.a ? "*" : "");
    textRight(SCREEN_W, y, pct);

    if (l.r[0]) {
      char reset[16];
      snprintf(reset, sizeof reset, "%sreset %s", l.s == 1 ? "! " : "", l.r);
      textAt(34, y + 8, reset);
    }
  }
}

void drawBreakdown() {
  textAt(0, HEADER_H, "Semaine par usage");

  const int y = HEADER_H + 10, w = SCREEN_W, h = 7;
  display.drawRect(0, y, w, h, SSD1306_WHITE);
  int x = 1;
  for (int i = 0; i < nShares; i++) {
    int sw = (w - 2) * constrain(shares[i].p, 0, 100) / 100;
    if (x + sw > w - 1) sw = w - 1 - x;
    fillPattern(x, y + 1, sw, h - 2, i);
    x += sw;
  }

  for (int i = 0; i < nShares && i < 4; i++) {
    int cx = (i % 2) * 64, cy = HEADER_H + 19 + (i / 2) * 9;
    display.drawRect(cx, cy, 6, 7, SSD1306_WHITE);
    fillPattern(cx + 1, cy + 1, 4, 5, i);
    char t[14];
    snprintf(t, sizeof t, "%s %d%%", shares[i].n, shares[i].p);
    textAt(cx + 8, cy, t);
  }

  if (hasSpend) {
    char t[24];
    if (spendOn) snprintf(t, sizeof t, "Credits %s/%s%s", spendUsed, spendMax, spendCur);
    else snprintf(t, sizeof t, "Credits : off");
    textAt(0, 56, t);
  }
}

void drawMessage() {
  if (!configured()) {
    textAt(0, 20, "Renseigne le WiFi");
    textAt(0, 32, "dans secrets.h");
  } else if (WiFi.status() != WL_CONNECTED) {
    textAt(0, 20, "Connexion WiFi...");
    textAt(0, 32, WIFI_SSID);
  } else if (!serverOk) {
    textAt(0, 18, "Serveur injoignable");
    textAt(0, 28, SERVER_URLS[urlIndex] + 7);   // sans "http://" (peut passer a la ligne)
    char ip[24];
    snprintf(ip, sizeof ip, "moi %s", WiFi.localIP().toString().c_str());
    textAt(0, 56, ip);
  } else {
    textAt(0, 20, "En attente...");
    textAt(0, 32, apiMsg);
  }
}

void render() {
  if (!oledOk) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // une page par limite, puis la repartition de la semaine
  int pages[8], n = 0;
  for (int i = 0; i < nLimits && n < 7; i++) pages[n++] = i;
  if (nShares > 0 || hasSpend) pages[n++] = -1;
  int current = (haveData && n) ? pages[page % n] : -1;
  focusLimit = current;

  drawHeader();
  if (!haveData) drawMessage();
  else if (current < 0) drawBreakdown();
  else drawLimits(current);

  display.display();
}

// ---------------------------------------------------------------------------
// Reseau
// ---------------------------------------------------------------------------

bool fetchFrom(const char *url) {
  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(5000);
  http.begin(url);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("GET %s -> %d\n", url, code);
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.printf("JSON invalide : %s\n", err.c_str());
    return false;
  }

  apiOk = (doc["ok"] | 0) != 0;
  strlcpy(apiMsg, doc["msg"] | "", sizeof apiMsg);
  strlcpy(plan, doc["plan"] | "", sizeof plan);
  ageAtFetch = doc["age"] | 0;
  fetchedAt = millis();

  nLimits = 0;
  for (JsonObject o : doc["L"].as<JsonArray>()) {
    if (nLimits >= 6) break;
    Limit &l = limits[nLimits++];
    strlcpy(l.n, o["n"] | "?", sizeof l.n);
    l.p = o["p"] | 0;
    strlcpy(l.r, o["r"] | "", sizeof l.r);
    l.s = o["s"] | 0;
    l.a = (o["a"] | 0) != 0;
  }

  nShares = 0;
  for (JsonObject o : doc["B"].as<JsonArray>()) {
    if (nShares >= 6) break;
    Share &s = shares[nShares++];
    strlcpy(s.n, o["n"] | "?", sizeof s.n);
    s.p = o["p"] | 0;
  }

  JsonObject x = doc["X"];
  hasSpend = !x.isNull();
  if (hasSpend) {
    spendOn = (x["on"] | 0) != 0;
    strlcpy(spendUsed, x["u"] | "", sizeof spendUsed);
    strlcpy(spendMax, x["m"] | "", sizeof spendMax);
    strlcpy(spendCur, x["c"] | "", sizeof spendCur);
  }

  haveData = nLimits > 0;
  Serial.printf("%s ok=%d limites=%d usages=%d age=%lds %s\n",
                url, apiOk, nLimits, nShares, ageAtFetch, apiMsg);
  return true;
}

// Essaie d'abord l'adresse qui a marche en dernier, puis l'autre
bool fetchData() {
  int count = sizeof(SERVER_URLS) / sizeof(SERVER_URLS[0]);
  for (int i = 0; i < count; i++) {
    int idx = (urlIndex + i) % count;
    if (fetchFrom(SERVER_URLS[idx])) {
      urlIndex = idx;
      serverOk = true;
      return true;
    }
  }
  serverOk = false;
  return false;
}

void keepWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiLogged) {
      Serial.printf("WiFi OK : IP %s, passerelle %s\n",
                    WiFi.localIP().toString().c_str(), WiFi.gatewayIP().toString().c_str());
      wifiLogged = true;
    }
    wifiLostAt = 0;
    return;
  }
  wifiLogged = false;
  if (wifiLostAt == 0) wifiLostAt = millis();
  if (millis() - wifiLostAt > WIFI_RETRY_MS) {
    Serial.println("WiFi : nouvelle tentative");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    wifiLostAt = millis();
  }
}

// ---------------------------------------------------------------------------

// Luminosite : 0 (tres faible) a 255 (max, defaut Adafruit ~207).
// Sous 64, la tension VCOMH est aussi abaissee pour attenuer vraiment l'ecran.
#define OLED_CONTRAST 8

void applyBrightness() {
  display.ssd1306_command(SSD1306_SETCONTRAST);
  display.ssd1306_command(OLED_CONTRAST);
  display.ssd1306_command(SSD1306_SETVCOMDETECT);
  display.ssd1306_command(OLED_CONTRAST < 64 ? 0x00 : 0x40);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\nClawdmeter OLED");

  oledOk = initOled();
  lastOledTry = millis();
  if (oledOk) {
    applyBrightness();
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    render();
  }

  if (configured()) {
    WiFi.mode(WIFI_STA);
    WiFi.setHostname("clawdmeter");
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

void loop() {
  unsigned long now = millis();

  // ecran absent au demarrage : on retente, la carte continue de tourner
  if (!oledOk && now - lastOledTry > 2000) {
    lastOledTry = now;
    oledOk = initOled();
    if (oledOk) {
      applyBrightness();
      Serial.println("OLED initialise");
    }
  }

  if (configured()) {
    keepWifi();
    if (WiFi.status() == WL_CONNECTED && now >= nextFetchAt) {
      nextFetchAt = now + (fetchData() ? FETCH_MS : RETRY_MS);
    }
  }

  if (now - pageAt >= PAGE_MS) {
    page++;
    pageAt = now;
  }

  render();
  delay(100);
}
