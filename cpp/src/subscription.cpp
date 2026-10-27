// LinkHelm — subscription handling (see subscription.h).
#include "subscription.h"

#include <winhttp.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <sstream>

#ifndef WINHTTP_NO_HEADER_DATA
#define WINHTTP_NO_HEADER_DATA NULL
#endif
#pragma comment(lib, "winhttp.lib")

namespace linkhelm {
namespace {

std::string Utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), out.data(), n,
                        nullptr, nullptr);
    return out;
}

std::wstring Utf16(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), n);
    return out;
}

void SplitUrl(const std::wstring& url, std::wstring* host, std::wstring* path,
              bool* https, INTERNET_PORT* port) {
    size_t p = url.find(L"://");
    std::wstring rest = (p == std::wstring::npos) ? url : url.substr(p + 3);
    *https = (p != std::wstring::npos) &&
             (url.substr(0, p) == L"https" || url.substr(0, p) == L"HTTPS");
    *port = *https ? INTERNET_DEFAULT_HTTPS_PORT : INTERNET_DEFAULT_HTTP_PORT;
    size_t slash = rest.find(L'/');
    *host = (slash == std::wstring::npos) ? rest : rest.substr(0, slash);
    *path = (slash == std::wstring::npos) ? L"/" : rest.substr(slash);
    size_t colon = host->rfind(L':');
    if (colon != std::wstring::npos && colon != host->size() - 1) {
        // no-exceptions build: wcstol never throws (unlike std::stoi).
        *port = (INTERNET_PORT)wcstol(host->substr(colon + 1).c_str(),
                                    nullptr, 10);
        *host = host->substr(0, colon);
    }
}

std::string HttpGet(const std::wstring& url, const std::wstring& proxy,
                    int timeout_ms, std::wstring* error) {
    std::wstring host, path;
    bool https = false;
    INTERNET_PORT port = 80;
    SplitUrl(url, &host, &path, &https, &port);

    // beta1.0.97 CRASH ROOT CAUSE (gdb-proven): WINHTTP_NO_PROXY_NAME and
    // WINHTTP_NO_PROXY_BYPASS are plain NULL macros. Initialising a
    // std::wstring from NULL throws std::logic_error ("basic_string:
    // construction from null"), and with -fno-exceptions that terminates the
    // process instantly — the whole "商店加速一点 Start 就退出" bug. Every
    // DownloadText call died here. Use empty strings; the empty() branch
    // below already maps back to the NULL macros for the WinHttp call.
    std::wstring proxy_arg;
    std::wstring proxy_bypass;
    DWORD access = WINHTTP_ACCESS_TYPE_NO_PROXY;
    std::wstring proxy_cfg;
    if (!proxy.empty()) {
        std::wstring p = proxy;
        if (p.rfind(L"http://", 0) == 0) p = p.substr(7);
        else if (p.rfind(L"https://", 0) == 0) p = p.substr(8);
        if (p.find(L"://") == std::wstring::npos &&
            p.find(L':') == std::wstring::npos) {
            p += L":7897";  // IP-only row -> default Clash mixed port
        }
        proxy_cfg = p;
        proxy_arg = proxy_cfg;
        access = WINHTTP_ACCESS_TYPE_NAMED_PROXY;
        proxy_bypass = L"<-loopback>";
    }

    HINTERNET sess = WinHttpOpen(L"LinkHelm/1.0 (ClashMeta)", access,
                                 proxy_arg.empty() ? WINHTTP_NO_PROXY_NAME
                                                   : proxy_arg.c_str(),
                                 proxy_bypass.c_str(), 0);
    if (!sess) {
        if (error)
            *error = L"WinHttpOpen failed (error " +
                     std::to_wstring(GetLastError()) + L")";
        return {};
    }
    // Panels answer with gzip when the client advertises it; without this
    // flag the body arrives as binary and every later sniff fails.
    DWORD decomp = WINHTTP_DECOMPRESSION_FLAG_GZIP |
                   WINHTTP_DECOMPRESSION_FLAG_DEFLATE;
    WinHttpSetOption(sess, WINHTTP_OPTION_DECOMPRESSION, &decomp,
                     sizeof(decomp));
    WinHttpSetTimeouts(sess, timeout_ms, timeout_ms, timeout_ms, timeout_ms);
    HINTERNET conn = WinHttpConnect(sess, host.c_str(), port, 0);
    if (!conn) {
        DWORD e = GetLastError();
        WinHttpCloseHandle(sess);
        if (error)
            *error = L"connect to " + host + L" failed (error " +
                     std::to_wstring(e) + L")";
        return {};
    }
    HINTERNET req = WinHttpOpenRequest(
        conn, L"GET", path.c_str(), nullptr, WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES, https ? WINHTTP_FLAG_SECURE : 0);
    if (!req) {
        WinHttpCloseHandle(conn);
        WinHttpCloseHandle(sess);
        if (error) *error = L"request failed";
        return {};
    }
    // Neutral browser UA. Some panels (e.g. xfltd) hard-block Clash-family
    // UAs (ClashforWindows / Clash Verge / Mihomo) with HTTP 403, which is
    // exactly what broke "paste the real subscription URL -> 0 nodes". A plain
    // browser UA is what v2rayN / NekoBox use and is accepted universally;
    // the returned body (base64 share links or Clash YAML) is parsed the same.
    LPCWSTR ua = L"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 L"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                 L"Safari/537.36\r\n"
                 L"Accept: */*";
    WinHttpAddRequestHeaders(req, ua, (DWORD)-1L, WINHTTP_ADDREQ_FLAG_ADD);

    std::string body;
    bool sent = WinHttpSendRequest(req, WINHTTP_NO_HEADER_DATA, 0,
                                   WINHTTP_NO_REQUEST_DATA, 0, 0, 0);
    if (!sent) {
        DWORD e = GetLastError();
        WinHttpCloseHandle(req);
        WinHttpCloseHandle(conn);
        WinHttpCloseHandle(sess);
        if (error)
            *error = L"request send failed (error " + std::to_wstring(e) + L")";
        return {};
    }
    bool ok = WinHttpReceiveResponse(req, nullptr) != FALSE;
    if (ok) {
        DWORD status = 0, sz = sizeof(status);
        WinHttpQueryHeaders(req,
                            WINHTTP_QUERY_STATUS_CODE |
                                WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &status, &sz,
                            WINHTTP_NO_HEADER_INDEX);
        DWORD avail = 0;
        while (WinHttpQueryDataAvailable(req, &avail) && avail > 0) {
            std::string chunk(avail, '\0');
            DWORD read = 0;
            if (WinHttpReadData(req, chunk.data(), avail, &read) && read > 0) {
                chunk.resize(read);
                body += chunk;
            }
        }
        if (status != 200 && error)
            *error = L"HTTP " + std::to_wstring(status) +
                     L" from " + host;
    } else if (error) {
        *error = L"response receive failed (error " +
                 std::to_wstring(GetLastError()) + L")";
    }
    WinHttpCloseHandle(req);
    WinHttpCloseHandle(conn);
    WinHttpCloseHandle(sess);
    return body;
}

bool LooksLikeBase64(const std::string& s) {
    size_t n = 0;
    for (char c : s) {
        if (std::isalnum((unsigned char)c) || c == '+' || c == '/' || c == '=') ++n;
    }
    return !s.empty() && n > s.size() * 0.9;
}

}  // namespace

bool DownloadText(const std::wstring& url, const std::wstring& proxy,
                  int timeout_ms, std::string* body, std::wstring* error) {
    std::string raw = HttpGet(url, proxy, timeout_ms, error);
    if (raw.empty()) return false;
    if (body) *body = raw;
    return true;
}

bool UnwrapClashLink(const std::wstring& link, std::wstring* inner_url) {
    auto pos = link.find(L"url=");
    if (pos == std::wstring::npos) return false;
    std::wstring enc = link.substr(pos + 4);
    size_t amp = enc.find(L'&');
    if (amp != std::wstring::npos) enc = enc.substr(0, amp);
    // percent-decode
    std::string bytes;
    for (size_t i = 0; i < enc.size(); ++i) {
        if (enc[i] == L'%' && i + 2 < enc.size()) {
            std::wstring hex = enc.substr(i + 1, 2);
            // no-exceptions build: wcstoul never throws (unlike std::stoul).
            unsigned long v = wcstoul(hex.c_str(), nullptr, 16);
            bytes.push_back((char)v);
            i += 2;
        } else {
            bytes.push_back((char)enc[i]);
        }
    }
    *inner_url = Utf16(bytes);
    return !inner_url->empty();
}

bool Base64Decode(const std::string& in, std::string* out) {
    static const std::string kTable =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string bits;
    for (char c : in) {
        if (c == '=' || c == '\n' || c == '\r' || c == ' ') continue;
        auto p = kTable.find(c);
        if (p == std::string::npos) return false;
        for (int i = 5; i >= 0; --i) bits.push_back(((char)p & (1 << i)) ? '1' : '0');
    }
    std::string res;
    for (size_t i = 0; i + 8 <= bits.size(); i += 8) {
        int v = 0;
        for (int j = 0; j < 8; ++j) v = (v << 1) | (bits[i + j] - '0');
        res.push_back((char)v);
    }
    *out = res;
    return !res.empty();
}

std::wstring Unquote(std::wstring value) {
    while (!value.empty() && (value.front() == L' ')) value.erase(value.begin());
    while (!value.empty() && (value.back() == L' ' || value.back() == L'\r')) value.pop_back();
    if (value.size() >= 2) {
        wchar_t first = value.front();
        if ((first == L'"' || first == L'\'') && value.back() == first) {
            return value.substr(1, value.size() - 2);
        }
    }
    return value;
}

std::vector<ProxyNode> ParseNodes(const std::string& text) {
    std::string body = text;
    if (LooksLikeBase64(body) && body.find("proxies:") == std::string::npos) {
        std::string decoded;
        if (Base64Decode(body, &decoded)) body = decoded;
    }
    std::wstring w = Utf16(body);
    std::vector<ProxyNode> nodes;
    // Flow-map style (- {name: 'x', type: ss, server: h, port: N, ...}) and
    // block style both work: we scan each "- " item and pull the keys.
    std::vector<std::wstring> items;
    size_t start = 0;
    for (size_t i = 0; i < w.size(); ++i) {
        if (w[i] == L'\n') {
            std::wstring line = w.substr(start, i - start);
            start = i + 1;
            size_t p = line.find(L"- ");
            if (p != std::wstring::npos && p < 4) {
                items.push_back(line);
            } else if (!items.empty()) {
                items.back() += L"\n" + line;
            }
        }
    }
    for (auto& item : items) {
        auto get = [&](const std::wstring& key) -> std::wstring {
            auto p = item.find(key + L":");
            if (p == std::wstring::npos) return {};
            p += key.size() + 1;
            size_t end = item.find_first_of(L",\n}", p);
            std::wstring v = (end == std::wstring::npos) ? item.substr(p)
                                                         : item.substr(p, end - p);
            return Unquote(v);
        };
        ProxyNode n;
        n.name = get(L"name");
        if (n.name.empty()) continue;
        std::wstring upper = n.name;
        std::transform(upper.begin(), upper.end(), upper.begin(), ::towupper);
        if (upper == L"DIRECT" || upper == L"REJECT" || upper == L"GLOBAL" ||
            upper == L"COMPATIBLE" || upper == L"PASS") {
            continue;
        }
        n.type = get(L"type");
        n.server = get(L"server");
        std::wstring port = get(L"port");
        if (!port.empty()) {
            // beta1.0.8: wcstol instead of stoi — no-exceptions build
            // (stoi would abort on a malformed port).
            n.port = (int)wcstol(port.c_str(), nullptr, 10);
            if (n.port < 0 || n.port > 65535) n.port = 0;
        }
        if (!n.server.empty() && n.port > 0) nodes.push_back(n);
    }
    return nodes;
}

std::string FirstSelectableGroup(const std::string& subscription_text) {
    std::wstring w = Utf16(subscription_text);
    auto p = w.find(L"proxy-groups:");
    if (p == std::wstring::npos) return {};
    std::wstring rest = w.substr(p);
    std::wstring line;
    std::wstring best;
    for (size_t i = 0; i < rest.size(); ++i) {
        if (rest[i] == L'\n' || i == rest.size() - 1) {
            if (i == rest.size() - 1) line.push_back(rest[i]);
            std::wstring trimmed = line;
            while (!trimmed.empty() && trimmed.front() == L' ') trimmed.erase(trimmed.begin());
            if (trimmed.rfind(L"- {", 0) == 0 || trimmed.rfind(L"- name:", 0) == 0) {
                auto np = trimmed.find(L"name:");
                if (np != std::wstring::npos) {
                    std::wstring name = Unquote(trimmed.substr(np + 5));
                    auto tp = trimmed.find(L"type:");
                    std::wstring type;
                    if (tp != std::wstring::npos) type = Unquote(trimmed.substr(tp + 5));
                    if (type == L"select" && best.empty()) best = name;
                }
            }
            line.clear();
        } else {
            line.push_back(rest[i]);
        }
    }
    return Utf8(best);
}

std::string BuildMihomoConfig(const std::string& subscription_text,
                              const std::string& group,
                              int mixed_port, int api_port, bool global_mode) {
    std::string body = subscription_text;
    if (LooksLikeBase64(body) && body.find("proxies:") == std::string::npos) {
        std::string decoded;
        if (Base64Decode(body, &decoded)) body = decoded;
    }
    std::ostringstream out;
    out << "# Generated by LinkHelm (C++ build) — loopback micro-kernel only.\n";
    out << "mixed-port: " << mixed_port << "\n";
    out << "allow-lan: false\n";
    out << "bind-address: 127.0.0.1\n";
    out << "external-controller: 127.0.0.1:" << api_port << "\n";
    out << "mode: " << (global_mode ? "global" : "rule") << "\n";
    out << "log-level: warning\n";
    out << "dns:\n  enable: false\n";
    out << body << "\n";
    if (!group.empty()) {
        out << "proxy-groups:\n";
        out << "  - name: \"" << group << "\"\n    type: select\n";
        out << "    proxies:\n";
        for (const auto& n : ParseNodes(subscription_text)) {
            out << "      - \"" << Utf8(n.name) << "\"\n";
        }
    }
    return out.str();
}

// ============== beta1.0.98: share links -> mihomo `proxies:` YAML ==========
// Everything below is pure string work: no exceptions, no stoi/stoul, no
// heap tricks — a malformed link can only return "" and be skipped.
namespace {

std::string UriDecode(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (size_t i = 0; i < in.size(); ++i) {
        if (in[i] == '%' && i + 2 < in.size() &&
            std::isxdigit((unsigned char)in[i + 1]) &&
            std::isxdigit((unsigned char)in[i + 2])) {
            char hex[3] = {in[i + 1], in[i + 2], 0};
            out.push_back((char)strtoul(hex, nullptr, 16));
            i += 2;
        } else {
            out.push_back(in[i]);
        }
    }
    return out;
}

std::string Yq(const std::string& s) {
    std::string out = "\"";
    for (char c : s) {
        if (c == '"' || c == '\\') out.push_back('\\');
        out.push_back(c);
    }
    out.push_back('"');
    return out;
}

struct Kv {
    std::string k, v;
};

std::vector<Kv> ParseQuery(const std::string& q) {
    std::vector<Kv> out;
    size_t pos = 0;
    while (pos < q.size()) {
        size_t amp = q.find('&', pos);
        std::string item = (amp == std::string::npos) ? q.substr(pos)
                                                     : q.substr(pos, amp - pos);
        pos = (amp == std::string::npos) ? q.size() : amp + 1;
        if (!item.empty()) {
            size_t eq = item.find('=');
            if (eq == std::string::npos) out.push_back({item, ""});
            else out.push_back({item.substr(0, eq), UriDecode(item.substr(eq + 1))});
        }
    }
    return out;
}

std::string QGet(const std::vector<Kv>& q, const char* key) {
    for (const auto& kv : q)
        if (kv.k == key) return kv.v;
    return std::string();
}

// host:port, tolerating [v6]:port and a missing port.
bool SplitHostPort(const std::string& hp, std::string* host, int* port) {
    if (hp.empty()) return false;
    std::string h = hp, p;
    if (hp[0] == '[') {
        size_t rb = hp.find(']');
        if (rb == std::string::npos) return false;
        h = hp.substr(1, rb - 1);
        p = (rb + 1 < hp.size() && hp[rb + 1] == ':') ? hp.substr(rb + 2) : "";
    } else {
        size_t colon = hp.rfind(':');
        if (colon == std::string::npos) return false;
        h = hp.substr(0, colon);
        p = hp.substr(colon + 1);
    }
    if (h.empty()) return false;
    int pn = p.empty() ? 443 : (int)strtoul(p.c_str(), nullptr, 10);
    if (pn <= 0 || pn > 65535) pn = 443;
    *host = h;
    *port = pn;
    return true;
}

bool TryBase64(const std::string& in, std::string* out) {
    std::string norm;
    norm.reserve(in.size());
    for (char c : in) {
        if (c == '-') norm.push_back('+');       // base64url
        else if (c == '_') norm.push_back('/');
        else norm.push_back(c);
    }
    return Base64Decode(norm, out);
}

bool Truthy(const std::string& v) { return v == "1" || v == "true"; }

// vless://<uuid>@<host>:<port>?type=tcp&security=reality&...#<name>
std::string VlessYaml(const std::string& body) {
    size_t at = body.find('@');
    if (at == std::string::npos) return {};
    std::string uuid = body.substr(0, at);
    std::string rest = body.substr(at + 1);
    std::string query, name;
    size_t hash = rest.find('#');
    if (hash != std::string::npos) {
        name = UriDecode(rest.substr(hash + 1));
        rest = rest.substr(0, hash);
    }
    size_t qm = rest.find('?');
    if (qm != std::string::npos) {
        query = rest.substr(qm + 1);
        rest = rest.substr(0, qm);
    }
    std::string host;
    int port = 0;
    if (!SplitHostPort(rest, &host, &port)) return {};
    auto q = ParseQuery(query);
    std::string net = QGet(q, "type");
    if (net.empty()) net = "tcp";
    std::string sec = QGet(q, "security");
    std::string flow = QGet(q, "flow");
    std::string fp = QGet(q, "fp");
    std::string sni = QGet(q, "sni");
    if (sni.empty()) sni = QGet(q, "peer");
    std::string pbk = QGet(q, "pbk");
    std::string sid = QGet(q, "sid");
    std::string hpath = QGet(q, "path");
    std::string hhost = QGet(q, "host");
    std::string svc = QGet(q, "serviceName");
    std::string insecure = QGet(q, "insecure");
    if (insecure.empty()) insecure = QGet(q, "allowInsecure");
    if (name.empty()) name = host + ":" + std::to_string(port);

    std::string y = "- {name: " + Yq(name) + ", type: vless, server: " + Yq(host) +
                    ", port: " + std::to_string(port) + ", uuid: " + Yq(uuid) +
                    ", udp: true";
    if (net != "tcp") y += ", network: " + net;
    bool tls = (sec == "tls" || sec == "reality" || sec == "xtls");
    if (tls) y += ", tls: true";
    if (sec == "reality") {
        y += ", reality-opts: {public-key: " + Yq(pbk) + ", short-id: " + Yq(sid) + "}";
        if (flow.empty()) flow = "xtls-rprx-vision";
    }
    if (!flow.empty()) y += ", flow: " + flow;
    if (!sni.empty()) y += ", servername: " + Yq(sni);
    if (!fp.empty()) y += ", client-fingerprint: " + fp;
    if (Truthy(insecure)) y += ", skip-cert-verify: true";
    if (net == "ws") {
        y += ", ws-opts: {path: " + Yq(hpath.empty() ? "/" : hpath);
        if (!hhost.empty()) y += ", headers: {Host: " + Yq(hhost) + "}";
        y += "}";
    } else if (net == "grpc") {
        y += ", grpc-opts: {grpc-service-name: " + Yq(svc) + "}";
    } else if (net == "h2" || net == "http") {
        y += ", h2-opts: {path: [" + Yq(hpath.empty() ? "/" : hpath) + "]";
        if (!hhost.empty()) y += ", host: [" + Yq(hhost) + "]";
        y += "}";
    }
    y += "}";
    return y;
}

// ss://<b64(method:pass)>@host:port#name  and  ss://<b64(method:pass@host:port)>#name
std::string SsYaml(const std::string& body) {
    std::string rest = body, name;
    size_t hash = rest.find('#');
    if (hash != std::string::npos) {
        name = UriDecode(rest.substr(hash + 1));
        rest = rest.substr(0, hash);
    }
    size_t qm = rest.find('?');               // plugin=… — ignored by design
    if (qm != std::string::npos) rest = rest.substr(0, qm);
    while (!rest.empty() && rest.back() == '/') rest.pop_back();

    std::string method, password, host, user;
    int port = 0;
    size_t at = rest.find('@');
    if (at != std::string::npos) {
        if (!SplitHostPort(rest.substr(at + 1), &host, &port)) return {};
        user = rest.substr(0, at);
        if (user.find(':') == std::string::npos) {
            std::string dec;
            if (TryBase64(user, &dec)) user = dec;
        } else {
            user = UriDecode(user);
        }
    } else {
        std::string dec;
        if (!TryBase64(rest, &dec)) return {};
        size_t at2 = dec.rfind('@');
        if (at2 == std::string::npos) return {};
        user = dec.substr(0, at2);
        if (!SplitHostPort(dec.substr(at2 + 1), &host, &port)) return {};
    }
    size_t colon = user.find(':');
    if (colon == std::string::npos) return {};
    method = user.substr(0, colon);
    password = user.substr(colon + 1);
    if (name.empty()) name = host + ":" + std::to_string(port);
    return "- {name: " + Yq(name) + ", type: ss, server: " + Yq(host) +
           ", port: " + std::to_string(port) + ", cipher: " + Yq(method) +
           ", password: " + Yq(password) + ", udp: true}";
}

std::string JsonField(const std::string& j, const char* key) {
    std::string pat = std::string("\"") + key + "\":\"";
    size_t p = j.find(pat);
    if (p != std::string::npos) {
        p += pat.size();
        size_t e = j.find('"', p);
        if (e == std::string::npos) return {};
        return j.substr(p, e - p);
    }
    pat = std::string("\"") + key + "\":";
    p = j.find(pat);
    if (p == std::string::npos) return {};
    p += pat.size();
    size_t e = j.find_first_of(",}", p);
    return j.substr(p, e == std::string::npos ? std::string::npos : e - p);
}

// vmess://<base64 of the classic JSON>
std::string VmessYaml(const std::string& body) {
    std::string dec;
    if (!TryBase64(body, &dec)) return {};
    std::string add = JsonField(dec, "add");
    std::string id = JsonField(dec, "id");
    if (add.empty() || id.empty()) return {};
    std::string port = JsonField(dec, "port");
    std::string aid = JsonField(dec, "aid");
    std::string net = JsonField(dec, "net");
    std::string tls = JsonField(dec, "tls");
    std::string host = JsonField(dec, "host");
    std::string path = JsonField(dec, "path");
    std::string sni = JsonField(dec, "sni");
    std::string ps = JsonField(dec, "ps");
    int pn = (int)strtoul(port.c_str(), nullptr, 10);
    if (pn <= 0 || pn > 65535) pn = 443;
    std::string name = ps.empty() ? add : ps;
    std::string y = "- {name: " + Yq(name) + ", type: vmess, server: " + Yq(add) +
                    ", port: " + std::to_string(pn) + ", uuid: " + Yq(id) +
                    ", alterId: " +
                    std::to_string(aid.empty() ? 0 : (int)strtoul(aid.c_str(), nullptr, 10)) +
                    ", cipher: auto, udp: true";
    if (tls == "tls") y += ", tls: true";
    if (!sni.empty()) y += ", servername: " + Yq(sni);
    if (net == "ws") {
        y += ", network: ws, ws-opts: {path: " + Yq(path.empty() ? "/" : path);
        if (!host.empty()) y += ", headers: {Host: " + Yq(host) + "}";
        y += "}";
    } else if (net == "grpc") {
        y += ", network: grpc, grpc-opts: {grpc-service-name: " + Yq(path) + "}";
    }
    y += "}";
    return y;
}

// trojan://<password>@<host>:<port>?sni=…&type=ws&path=…#<name>
std::string TrojanYaml(const std::string& body) {
    size_t at = body.find('@');
    if (at == std::string::npos) return {};
    std::string pass = UriDecode(body.substr(0, at));
    std::string rest = body.substr(at + 1);
    std::string query, name;
    size_t hash = rest.find('#');
    if (hash != std::string::npos) {
        name = UriDecode(rest.substr(hash + 1));
        rest = rest.substr(0, hash);
    }
    size_t qm = rest.find('?');
    if (qm != std::string::npos) {
        query = rest.substr(qm + 1);
        rest = rest.substr(0, qm);
    }
    std::string host;
    int port = 0;
    if (!SplitHostPort(rest, &host, &port)) return {};
    auto q = ParseQuery(query);
    std::string sni = QGet(q, "sni");
    if (sni.empty()) sni = QGet(q, "peer");
    std::string net = QGet(q, "type");
    std::string hpath = QGet(q, "path");
    std::string hhost = QGet(q, "host");
    std::string svc = QGet(q, "serviceName");
    std::string insecure = QGet(q, "allowInsecure");
    if (insecure.empty()) insecure = QGet(q, "insecure");
    std::string fp = QGet(q, "fp");
    if (name.empty()) name = host + ":" + std::to_string(port);

    std::string y = "- {name: " + Yq(name) + ", type: trojan, server: " + Yq(host) +
                    ", port: " + std::to_string(port) + ", password: " + Yq(pass) +
                    ", udp: true";
    if (!sni.empty()) y += ", sni: " + Yq(sni);
    if (!fp.empty()) y += ", client-fingerprint: " + fp;
    if (Truthy(insecure)) y += ", skip-cert-verify: true";
    if (net == "ws") {
        y += ", network: ws, ws-opts: {path: " + Yq(hpath.empty() ? "/" : hpath);
        if (!hhost.empty()) y += ", headers: {Host: " + Yq(hhost) + "}";
        y += "}";
    } else if (net == "grpc") {
        y += ", network: grpc, grpc-opts: {grpc-service-name: " + Yq(svc) + "}";
    }
    y += "}";
    return y;
}

// hysteria2://<password>@<host>:<port>?sni=…&insecure=1&obfs=…#<name>
std::string Hy2Yaml(const std::string& body) {
    size_t at = body.find('@');
    if (at == std::string::npos) return {};
    std::string pass = UriDecode(body.substr(0, at));
    std::string rest = body.substr(at + 1);
    std::string query, name;
    size_t hash = rest.find('#');
    if (hash != std::string::npos) {
        name = UriDecode(rest.substr(hash + 1));
        rest = rest.substr(0, hash);
    }
    size_t qm = rest.find('?');
    if (qm != std::string::npos) {
        query = rest.substr(qm + 1);
        rest = rest.substr(0, qm);
    }
    std::string host;
    int port = 0;
    if (!SplitHostPort(rest, &host, &port)) return {};
    auto q = ParseQuery(query);
    std::string sni = QGet(q, "sni");
    std::string insecure = QGet(q, "insecure");
    std::string obfs = QGet(q, "obfs");
    std::string obfs_pw = QGet(q, "obfs-password");
    if (name.empty()) name = host + ":" + std::to_string(port);
    std::string y = "- {name: " + Yq(name) + ", type: hysteria2, server: " +
                    Yq(host) + ", port: " + std::to_string(port) +
                    ", password: " + Yq(pass);
    if (!sni.empty()) y += ", sni: " + Yq(sni);
    if (Truthy(insecure)) y += ", skip-cert-verify: true";
    if (!obfs.empty()) {
        y += ", obfs: " + Yq(obfs);
        if (!obfs_pw.empty()) y += ", obfs-password: " + Yq(obfs_pw);
    }
    y += "}";
    return y;
}

// tuic://<uuid>:<password>@<host>:<port>?sni=…&alpn=…#<name>
std::string TuicYaml(const std::string& body) {
    size_t at = body.find('@');
    if (at == std::string::npos) return {};
    std::string user = UriDecode(body.substr(0, at));
    size_t colon = user.find(':');
    if (colon == std::string::npos) return {};
    std::string uuid = user.substr(0, colon), pass = user.substr(colon + 1);
    std::string rest = body.substr(at + 1);
    std::string query, name;
    size_t hash = rest.find('#');
    if (hash != std::string::npos) {
        name = UriDecode(rest.substr(hash + 1));
        rest = rest.substr(0, hash);
    }
    size_t qm = rest.find('?');
    if (qm != std::string::npos) {
        query = rest.substr(qm + 1);
        rest = rest.substr(0, qm);
    }
    std::string host;
    int port = 0;
    if (!SplitHostPort(rest, &host, &port)) return {};
    auto q = ParseQuery(query);
    std::string sni = QGet(q, "sni");
    std::string cc = QGet(q, "congestion_control");
    std::string insecure = QGet(q, "allow_insecure");
    if (name.empty()) name = host + ":" + std::to_string(port);
    std::string y = "- {name: " + Yq(name) + ", type: tuic, server: " + Yq(host) +
                    ", port: " + std::to_string(port) + ", uuid: " + Yq(uuid) +
                    ", password: " + Yq(pass) + ", udp: true";
    if (!sni.empty()) y += ", sni: " + Yq(sni);
    if (!cc.empty()) y += ", congestion-controller: " + Yq(cc);
    if (Truthy(insecure)) y += ", skip-cert-verify: true";
    y += "}";
    return y;
}

}  // namespace

bool BuildProxiesYamlFromLinks(const std::string& content, std::string* yaml,
                               int* count) {
    if (yaml) yaml->clear();
    if (count) *count = 0;
    std::string body = content;
    if (LooksLikeBase64(body) && body.find("://") == std::string::npos) {
        std::string dec;
        if (Base64Decode(body, &dec) && !dec.empty()) body = dec;
    }
    std::string out;
    int n = 0;
    size_t pos = 0;
    while (pos < body.size()) {
        size_t nl = body.find('\n', pos);
        std::string line = (nl == std::string::npos) ? body.substr(pos)
                                                    : body.substr(pos, nl - pos);
        pos = (nl == std::string::npos) ? body.size() : nl + 1;
        // trim CR, spaces and a stray BOM
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ' ||
                                 line.back() == '\t'))
            line.pop_back();
        size_t st = 0;
        while (st < line.size() && (line[st] == ' ' || line[st] == '\t')) ++st;
        line = line.substr(st);
        if (line.size() >= 3 && (unsigned char)line[0] == 0xEF &&
            (unsigned char)line[1] == 0xBB && (unsigned char)line[2] == 0xBF)
            line = line.substr(3);
        if (line.empty()) continue;

        std::string y;
        if (line.rfind("vless://", 0) == 0) y = VlessYaml(line.substr(8));
        else if (line.rfind("vmess://", 0) == 0) y = VmessYaml(line.substr(8));
        else if (line.rfind("trojan://", 0) == 0) y = TrojanYaml(line.substr(9));
        else if (line.rfind("hysteria2://", 0) == 0) y = Hy2Yaml(line.substr(12));
        else if (line.rfind("hy2://", 0) == 0) y = Hy2Yaml(line.substr(6));
        else if (line.rfind("tuic://", 0) == 0) y = TuicYaml(line.substr(7));
        else if (line.rfind("ss://", 0) == 0) y = SsYaml(line.substr(5));
        if (!y.empty()) {
            out += y;
            out += '\n';
            ++n;
        }
    }
    if (yaml) *yaml = out;
    if (count) *count = n;
    return n > 0;
}

}  // namespace linkhelm
