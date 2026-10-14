// Copy this file to secrets.h and fill in your own values.
// secrets.h is git-ignored on purpose: it holds your Wi-Fi password.
//
// Note: the ESP32 only joins 2.4 GHz networks, not 5 GHz.
#pragma once

#define WIFI_SSID  "YOUR_WIFI"
#define WIFI_PASS  "YOUR_PASSWORD"

// Address of the PC running clawdmeter_server.py.
// If your PC has several IPs (one per subnet), list two: the board keeps
// whichever answers first and remembers it.
#define SERVER_URL     "http://192.168.1.10:8080/esp.json"
#define SERVER_URL_ALT "http://192.168.1.10:8080/esp.json"
