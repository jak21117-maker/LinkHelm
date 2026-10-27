// LinkHelm — subscription handling in C++ (replaces store_accel.py's
// parsing/config side, keeping only the parts C++ actually does better:
// no interpreter start-up, no GIL, timeouts we control).
#pragma once

#include <windows.h>

#include <string>
#include <vector>

namespace linkhelm {

struct ProxyNode {
    std::wstring name;
    std::wstring type;
    std::wstring server;
    int port = 0;
};

// Downloads a subscription. proxy may be "" (direct) or "host:port" /
// "http://host:port" — panels that bind the token to the fetching IP need
// the request to leave through the user's Clash.
bool DownloadText(const std::wstring& url, const std::wstring& proxy,
                  int timeout_ms, std::string* body, std::wstring* error);

// Unwraps clash://install-config?url=<urlencoded>&name=...
bool UnwrapClashLink(const std::wstring& link, std::wstring* inner_url);

bool Base64Decode(const std::string& in, std::string* out);

// Accepts raw YAML, base64 YAML, or a base64 subscription list.
std::vector<ProxyNode> ParseNodes(const std::string& text);

// Builds the mihomo config: loopback mixed port + API, our own rules only,
// the user's own subscription untouched otherwise.
std::string BuildMihomoConfig(const std::string& subscription_text,
                              const std::string& group,
                              int mixed_port, int api_port,
                              bool global_mode);

std::string FirstSelectableGroup(const std::string& subscription_text);

// beta1.0.98: convert a share-link list (vless://, ss://, vmess://,
// trojan://, hysteria2://, tuic://) into mihomo `proxies:` YAML lines.
// The bundled kernel parses YAML proxy objects natively but NOT bare share
// links in proxy-providers, so parsing stays fully in-process: no extra
// 46MB kernel, no python, and a malformed link skips one line instead of
// aborting the UI.
bool BuildProxiesYamlFromLinks(const std::string& content,
                               std::string* yaml, int* count);

// YAML-ish quoting: airports single-quote node names ('HK 01 [V]'), so the
// exact same quote character must be stripped (a naive strip of only the
// double quote is what made mihomo fail to find every node in 1.0.62).
std::wstring Unquote(std::wstring value);

}  // namespace linkhelm
