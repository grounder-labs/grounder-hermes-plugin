// Copyright 2026 Grounder Labs, Inc
// SPDX-License-Identifier: Apache-2.0
//
// Grounder direct: read one page in a hidden WebKit view, asked for with the user agent given (Safari's own), and print it as one JSON line.
//
//   direct-reader <url> <user agent> [most characters of text]   read one page
//   direct-reader --signin <site> <user agent> [start address]   open a window to sign in to a site, and save that sign-in
//   direct-reader --paste <site>                                 save a Cookie header for a site, typed or pasted at a hidden prompt
//
// With GROUNDER_DIRECT_SIGNED_OUT=1 a read uses no saved sign-in at all: nothing is read from the keychain or the sealed file, and nothing is saved back.
//
// With GROUNDER_DIRECT_RESOLVER set to a DNS-over-HTTPS address (Grounder's, when the user picks it), pages are looked up there instead of through this Mac's
// resolver, so a filter on the Mac or its network does not decide what loads. The pages themselves still go straight from this Mac to each site, encrypted end
// to end: the resolver learns which hosts are read, never what is on them. An answer that points at a private, loopback or link-local address is refused.
//
// Each read starts a fresh, empty session. Where the user saved a sign-in for the page's site, that site's cookies are set in it before the page is asked for, so
// the site sees the user signed in, with the same user agent they signed in with; cookies the site changes while reading are saved back, so the sign-in stays
// fresh. Nothing is kept for any other site. Each site's cookies are sealed (AES-GCM, the site's name authenticated with them) in a file only this user can read,
// ~/Library/Application Support/Grounder Direct/sign-ins/<site>.sealed, under a key of its own that only the login keychain holds (service "grounder-direct",
// one item a site, written through /usr/bin/security on its standard input). No cookie value is ever on a command line, in a log or in the output: output names a
// site and counts its cookies. A site's own checks run as in any browser; nothing here solves or skips one.

import AppKit
import CryptoKit
import Network
import WebKit

let SERVICE = "grounder-direct"
let LEGACY_SERVICE = "grounder-direct-dev"  // 0.8.0-dev.2 kept a site's whole sign-in in the item; still read, and moved to SERVICE when next saved
let SECURITY = "/usr/bin/security"
/// The home directory: ``HOME`` when it names one (so a test or a second profile can point the reader at its own store), else the user's own.
func homeDirectory() -> URL {
    if let home = ProcessInfo.processInfo.environment["HOME"], home.hasPrefix("/") { return URL(fileURLWithPath: home, isDirectory: true) }
    return FileManager.default.homeDirectoryForCurrentUser
}
let STORE = homeDirectory().appendingPathComponent("Library/Application Support/Grounder Direct/sign-ins")
let NOT_FOUND: Int32 = 44  // errSecItemNotFound, as security exits

func printLine(_ object: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{\"error\":\"could not encode the result\"}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

// MARK: Saved sign-ins

/// Run /usr/bin/security with ``input`` on its standard input. It starts in a session of its own, with no terminal: run from a terminal it would otherwise ask
/// that terminal for a key, and wait there, instead of reading what it is given.
func security(_ arguments: [String], input: String? = nil) -> (status: Int32, out: String) {
    var stdinPipe: [Int32] = [0, 0], stdoutPipe: [Int32] = [0, 0]
    guard pipe(&stdinPipe) == 0, pipe(&stdoutPipe) == 0 else { return (-1, "") }
    var actions: posix_spawn_file_actions_t?
    posix_spawn_file_actions_init(&actions)
    posix_spawn_file_actions_adddup2(&actions, stdinPipe[0], STDIN_FILENO)
    posix_spawn_file_actions_adddup2(&actions, stdoutPipe[1], STDOUT_FILENO)
    posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0)
    for fd in stdinPipe + stdoutPipe { posix_spawn_file_actions_addclose(&actions, fd) }
    var attributes: posix_spawnattr_t?
    posix_spawnattr_init(&attributes)
    posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETSID))
    defer { posix_spawn_file_actions_destroy(&actions); posix_spawnattr_destroy(&attributes) }
    let argv = ([SECURITY] + arguments).map { strdup($0) } + [nil]
    defer { argv.forEach { free($0) } }
    var pid: pid_t = 0
    let spawned = posix_spawn(&pid, SECURITY, &actions, &attributes, argv, environ)
    close(stdinPipe[0]); close(stdoutPipe[1])
    guard spawned == 0 else { close(stdinPipe[1]); close(stdoutPipe[0]); return (-1, "") }
    if let input = input { _ = input.withCString { write(stdinPipe[1], $0, strlen($0)) } }
    close(stdinPipe[1])
    let out = FileHandle(fileDescriptor: stdoutPipe[0], closeOnDealloc: true).readDataToEndOfFile()
    var status: Int32 = 0
    waitpid(pid, &status, 0)
    return ((status >> 8) & 0xff, String(decoding: out, as: UTF8.self))
}

/// The site a cookie domain belongs to: the site itself, one of its subdomains, or a parent domain of it.
func belongs(_ domain: String, to site: String) -> Bool {
    let bare = domain.hasPrefix(".") ? String(domain.dropFirst()) : domain
    return bare == site || bare.hasSuffix("." + site) || site.hasSuffix("." + bare)
}

/// The sites a host's saved sign-in may be filed under, nearest first: ``old.reddit.com``, then ``reddit.com``.
func sitesFor(_ host: String) -> [String] {
    var labels = host.lowercased().split(separator: ".").map(String.init)
    var out: [String] = []
    while labels.count >= 2 {
        out.append(labels.joined(separator: "."))
        labels.removeFirst()
    }
    return out
}

func cookieRecord(_ cookie: HTTPCookie) -> [String: Any] {
    var record: [String: Any] = ["name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path,
                                 "secure": cookie.isSecure, "httpOnly": cookie.isHTTPOnly]
    if let expires = cookie.expiresDate { record["expires"] = expires.timeIntervalSince1970 }
    if let policy = cookie.sameSitePolicy { record["sameSite"] = policy.rawValue }
    return record
}

func cookie(from record: [String: Any]) -> HTTPCookie? {
    guard let name = record["name"] as? String, let value = record["value"] as? String, let domain = record["domain"] as? String else { return nil }
    var properties: [HTTPCookiePropertyKey: Any] = [.name: name, .value: value, .domain: domain, .path: (record["path"] as? String) ?? "/"]
    if record["secure"] as? Bool == true { properties[.secure] = "TRUE" }
    if record["httpOnly"] as? Bool == true { properties[HTTPCookiePropertyKey("HttpOnly")] = "TRUE" }
    if let expires = record["expires"] as? Double {
        if expires < Date().timeIntervalSince1970 { return nil }  // expired: the site would not take it either
        properties[.expires] = Date(timeIntervalSince1970: expires)
    }
    if let policy = record["sameSite"] as? String { properties[.sameSitePolicy] = policy }
    return HTTPCookie(properties: properties)
}

/// One cookie's identity and value, to tell whether a site changed its cookies during a read.
func fingerprint(_ records: [[String: Any]]) -> [String] {
    records.map { "\($0["domain"] ?? "")|\($0["path"] ?? "")|\($0["name"] ?? "")=\($0["value"] ?? "")" }.sorted()
}

enum Lookup {
    case none
    case found(site: String, cookies: [HTTPCookie], records: [[String: Any]])
    case failed(String)
}

func sealedFile(_ site: String) -> URL { STORE.appendingPathComponent("\(site).sealed") }

func records(from json: Data) -> [[String: Any]]? {
    ((try? JSONSerialization.jsonObject(with: json)) as? [String: Any])?["cookies"] as? [[String: Any]]
}

enum Key {
    case none
    case key(SymmetricKey)
    case legacy([[String: Any]])
    case failed(String)
}

/// A site's key from the login keychain; or, from 0.8.0-dev.2, the sign-in itself. A keychain that cannot be read is a failure, not "no sign-in".
func key(for site: String) -> Key {
    let locked = { (status: Int32) in "could not read the sign-in saved for \(site) from the login keychain (security exited \(status); is the keychain locked?)" }
    let (status, out) = security(["find-generic-password", "-s", SERVICE, "-a", site, "-w"])
    if status == 0 {
        guard let raw = Data(base64Encoded: out.trimmingCharacters(in: .whitespacesAndNewlines)), raw.count == 32 else {
            return .failed("the key saved for \(site) is not one this reader made; sign in again with /search-login \(site)")
        }
        return .key(SymmetricKey(data: raw))
    }
    if status != NOT_FOUND { return .failed(locked(status)) }
    let (old, legacy) = security(["find-generic-password", "-s", LEGACY_SERVICE, "-a", site, "-w"])
    if old == NOT_FOUND { return .none }
    if old != 0 { return .failed(locked(old)) }
    guard let json = Data(base64Encoded: legacy.trimmingCharacters(in: .whitespacesAndNewlines)), let found = records(from: json) else {
        return .failed("the sign-in saved for \(site) could not be read; sign in again with /search-login \(site)")
    }
    return .legacy(found)
}

/// The sign-in saved for a host, if there is one, nearest site first.
func savedSignIn(for host: String) -> Lookup {
    for site in sitesFor(host) {
        var found: [[String: Any]]
        switch key(for: site) {
        case .none: continue
        case .failed(let why): return .failed(why)
        case .legacy(let old): found = old
        case .key(let key):
            guard let sealed = try? Data(contentsOf: sealedFile(site)) else {
                return .failed("the keychain has a key for \(site) but its sealed sign-in is missing; sign in again with /search-login \(site)")
            }
            guard let box = try? AES.GCM.SealedBox(combined: sealed), let json = try? AES.GCM.open(box, using: key, authenticating: Data(site.utf8)),
                  let opened = records(from: json) else {
                return .failed("the sealed sign-in for \(site) does not open with its key; sign in again with /search-login \(site)")
            }
            found = opened
        }
        return .found(site: site, cookies: found.compactMap(cookie(from:)), records: found)
    }
    return .none
}

/// A site's cookies, each made to go to every subdomain and every page of the site (``.reddit.com``, path ``/``), so one sign-in covers the whole site. A
/// ``__Host-`` cookie stays as the site set it: a browser takes one only for its own host. Where two become the same cookie, the one the site scoped most
/// narrowly wins, as a browser would send it there.
func widened(_ records: [[String: Any]], to site: String) -> [[String: Any]] {
    var kept: [String: (rank: Int, record: [String: Any])] = [:]
    for record in records {
        guard let name = record["name"] as? String else { continue }
        let domain = (record["domain"] as? String) ?? site
        let rank = domain.count * 1000 + ((record["path"] as? String) ?? "/").count
        var wide = record
        if !name.hasPrefix("__Host-") {
            wide["domain"] = "." + site
            wide["path"] = "/"
        }
        let id = "\(wide["domain"] ?? "")|\(wide["path"] ?? "")|\(name)"
        if let held = kept[id], held.rank >= rank { continue }
        kept[id] = (rank, wide)
    }
    return kept.values.map { $0.record }
}

/// The cookies to set for one read. WebKit does not send a cookie set here for ``.reddit.com`` to ``www.reddit.com`` (checked 2026-11-19), only to the host
/// named, so each site-wide cookie is set instead for the host being read, and for the site and its ``www.``, between which sites commonly redirect.
func forHosts(_ cookies: [HTTPCookie], site: String, host: String) -> [HTTPCookie] {
    var out = cookies.filter { !$0.domain.hasPrefix(".") || $0.name.hasPrefix("__Host-") }  // site-wide ones are replaced by their per-host copies
    for cookie in cookies where cookie.domain.hasPrefix(".") && !cookie.name.hasPrefix("__Host-") {
        for name in Set([host.lowercased(), site, "www." + site]) {
            var properties = cookie.properties ?? [:]
            properties[.domain] = name
            if let copy = HTTPCookie(properties: properties) { out.append(copy) }
        }
    }
    return out
}

/// Save a site's cookies, replacing what was saved for it: sealed in its file under its key, the key (new the first time) in the login keychain. Returns why
/// not, or nil.
func saveSignIn(site: String, records raw: [[String: Any]], how: String) -> String? {
    let records = widened(raw, to: site)
    let now = ISO8601DateFormatter().string(from: Date())
    guard let json = try? JSONSerialization.data(withJSONObject: ["site": site, "saved": now, "how": how, "cookies": records]) else {
        return "could not encode the cookies"
    }
    var siteKey: SymmetricKey
    switch key(for: site) {
    case .key(let existing): siteKey = existing
    case .failed(let why): return why
    case .none, .legacy: siteKey = SymmetricKey(size: .bits256)
    }
    let encoded = siteKey.withUnsafeBytes { Data($0) }.base64EncodedString()
    // The key goes on security's standard input (asked for twice, as it asks a person), never on its command line. The comment names no value.
    let (status, _) = security(["add-generic-password", "-U", "-s", SERVICE, "-a", site, "-l", "Grounder direct sign-in key: \(site)",
                                "-j", "saved \(now) by \(how), \(records.count) cookies", "-w"], input: "\(encoded)\n\(encoded)\n")
    if status != 0 { return "security exited \(status) saving the key to the login keychain" }
    do {
        try FileManager.default.createDirectory(at: STORE, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let sealed = try AES.GCM.seal(json, using: siteKey, authenticating: Data(site.utf8))
        guard let combined = sealed.combined else { return "could not seal the cookies" }
        let partial = STORE.appendingPathComponent(".\(site).\(UUID().uuidString).partial")
        FileManager.default.createFile(atPath: partial.path, contents: nil, attributes: [.posixPermissions: 0o600])
        try combined.write(to: partial)
        _ = try FileManager.default.replaceItemAt(sealedFile(site), withItemAt: partial)
    } catch {
        return "could not write the sealed sign-in: \(error.localizedDescription)"
    }
    _ = security(["delete-generic-password", "-s", LEGACY_SERVICE, "-a", site])  // moved: the 0.8.0-dev.2 item, if there was one, is no longer needed
    return nil
}

/// A line typed or pasted at a hidden prompt on the terminal. Raw, not line mode: macOS cuts a line-mode line at 1024 characters, and a Cookie header is longer.
func hiddenLine(_ prompt: String) -> String? {
    let tty = open("/dev/tty", O_RDWR)
    guard tty >= 0 else { return nil }
    defer { close(tty) }
    var saved = termios()
    tcgetattr(tty, &saved)
    var raw = saved
    raw.c_lflag &= ~tcflag_t(ECHO | ICANON)
    tcsetattr(tty, TCSAFLUSH, &raw)
    defer { tcsetattr(tty, TCSAFLUSH, &saved); _ = write(tty, "\n", 1) }
    _ = prompt.withCString { write(tty, $0, strlen($0)) }
    var bytes: [UInt8] = []
    var byte: UInt8 = 0
    while read(tty, &byte, 1) == 1 {
        if byte == 10 || byte == 13 { break }
        if byte == 3 || byte == 4 { return nil }  // Control-C, Control-D: nothing saved
        if byte == 127 || byte == 8 { if !bytes.isEmpty { bytes.removeLast() }; continue }
        bytes.append(byte)
    }
    return String(decoding: bytes, as: UTF8.self)
}

// MARK: Which addresses may be read

// The reader refuses the same addresses the calling program does, and it does so where the traffic happens: at every navigation of the page (the address typed, a
// redirect, a script or a meta refresh) and, when it can, at every connection (see ResolvingTunnel). The tables live here so that both builds have them.

/// The 16 bytes of an IPv6 address, or the 4 of an IPv4 one, from a host written as ``ipaddress.ip_address`` or ``socket.inet_aton`` reads it; nil if neither.
func ipBytes(_ host: String) -> [UInt8]? {
    func strictV4(_ text: String) -> [UInt8]? {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var out: [UInt8] = []
        for part in parts {
            guard !part.isEmpty, part.count <= 3, part.allSatisfy({ $0.isASCII && $0.isNumber }), !(part.count > 1 && part.hasPrefix("0")), let value = UInt8(String(part)) else { return nil }
            out.append(value)
        }
        return out
    }
    if let v4 = strictV4(host) { return v4 }
    var six = in6_addr()
    let bare = host.split(separator: "%", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? host
    if host.contains(":"), inet_pton(AF_INET6, bare, &six) == 1 { return withUnsafeBytes(of: &six) { Array($0) } }
    var four = in_addr()
    if inet_aton(host, &four) == 1 { return withUnsafeBytes(of: &four) { Array($0) } }
    return nil
}

private func inNet(_ b: [UInt8], _ prefix: [UInt8], _ bits: Int) -> Bool {
    for i in 0..<bits {
        let mask = UInt8(0x80 >> (i % 8))
        if (b[i / 8] & mask) != (prefix[i / 8] & mask) { return false }
    }
    return true
}

private func v6(_ text: String) -> [UInt8] { var a = in6_addr(); inet_pton(AF_INET6, text, &a); return withUnsafeBytes(of: &a) { Array($0) } }

/// ``ipaddress``'s ``is_global`` (Python 3.14) for an IPv4 address's bytes or an IPv6 address's.
func isGlobal(_ b: [UInt8]) -> Bool {
    if b.count == 4 {
        let nets: [([UInt8], Int)] = [([0, 0, 0, 0], 8), ([10, 0, 0, 0], 8), ([127, 0, 0, 0], 8), ([169, 254, 0, 0], 16), ([172, 16, 0, 0], 12),
                                      ([192, 0, 0, 0], 24), ([192, 0, 2, 0], 24), ([192, 168, 0, 0], 16), ([198, 18, 0, 0], 15),
                                      ([198, 51, 100, 0], 24), ([203, 0, 113, 0], 24), ([240, 0, 0, 0], 4), ([255, 255, 255, 255], 32)]
        if inNet(b, [100, 64, 0, 0], 10) { return false }
        if inNet(b, [192, 0, 0, 9], 32) || inNet(b, [192, 0, 0, 10], 32) { return true }
        return !nets.contains { inNet(b, $0.0, $0.1) }
    }
    let nets: [(String, Int)] = [("::1", 128), ("::", 128), ("::ffff:0:0", 96), ("64:ff9b:1::", 48), ("100::", 64), ("2001::", 23), ("2001:db8::", 32),
                                 ("2002::", 16), ("3fff::", 20), ("fc00::", 7), ("fe80::", 10)]
    let exceptions: [(String, Int)] = [("2001:1::1", 128), ("2001:1::2", 128), ("2001:3::", 32), ("2001:4:112::", 48), ("2001:20::", 28), ("2001:30::", 28)]
    if exceptions.contains(where: { inNet(b, v6($0.0), $0.1) }) { return true }
    return !nets.contains { inNet(b, v6($0.0), $0.1) }
}

/// A host as a browser will read it, which is not always as it is written: percent-escapes decoded, compatibility characters (fullwidth and circled digits, the
/// ideographic full stops) mapped to their plain forms, and the characters a browser ignores dropped. ``①②⑦.0.0.1`` and ``１２７.０.０.１`` are 127.0.0.1 to WebKit.
func foldedHost(_ raw: String) -> String {
    var folded = (raw.removingPercentEncoding ?? raw).precomposedStringWithCompatibilityMapping.lowercased()
    for dot in ["\u{3002}", "\u{FF0E}", "\u{FF61}"] { folded = folded.replacingOccurrences(of: dot, with: ".") }
    let ignored: Set<Unicode.Scalar> = ["\u{00AD}", "\u{200B}", "\u{200C}", "\u{200D}", "\u{2060}", "\u{FEFF}"]
    return String(String.UnicodeScalarView(folded.unicodeScalars.filter { !ignored.contains($0) }))
}

/// Whether an address (its bytes) is on the public internet: global by ``ipaddress``'s reckoning (which the plugin's own refusal uses, multicast included), with an
/// IPv4-mapped (``::ffff:a.b.c.d``) spelling judged as the IPv4 address in it and an IPv4-compatible one (``::a.b.c.d``) refused.
func publicAddress(_ bytes: [UInt8]) -> Bool {
    var b = bytes
    if b.count == 16 {
        if Array(b[0..<12]).allSatisfy({ $0 == 0 }) { return false }  // ::a.b.c.d (IPv4-compatible, and the unspecified and loopback addresses)
        if Array(b[0..<10]).allSatisfy({ $0 == 0 }), b[10] == 0xFF, b[11] == 0xFF { b = Array(b[12...]) }  // ::ffff:127.0.0.1 is the 127.0.0.1 in it
    }
    return isGlobal(b)
}

/// Whether a host name is a private, loopback, link-local or otherwise non-public address, or a name only a local network has. A host is judged as written AND as a
/// browser would read it (``foldedHost``); one with a ``%`` in it is refused outright, since only a browser's decoding could make it mean anything else.
func privateHost(_ rawHost: String) -> Bool {
    func privateName(_ name: String) -> Bool {
        var host = name.lowercased()
        while host.hasSuffix(".") { host.removeLast() }
        if host.isEmpty || host == "localhost" || [".localhost", ".local", ".internal", ".lan", ".home.arpa"].contains(where: { host.hasSuffix($0) })
            || (!host.contains(".") && !host.contains(":")) { return true }
        guard let bytes = ipBytes(host) else { return false }
        return !publicAddress(bytes)
    }
    if rawHost.contains("%") { return true }
    let folded = foldedHost(rawHost)
    return privateName(rawHost) || (folded != rawHost.lowercased() && privateName(folded))
}

/// Whether the host's name resolves, on this Mac, to an address that is not a public one.
func resolvesToPrivate(_ host: String) -> Bool {
    var hints = addrinfo()
    hints.ai_family = AF_UNSPEC
    hints.ai_socktype = SOCK_STREAM
    var result: UnsafeMutablePointer<addrinfo>?
    guard getaddrinfo(host, nil, &hints, &result) == 0, let first = result else { return false }  // a name that does not resolve is left to the read, which says so
    defer { freeaddrinfo(first) }
    var node: UnsafeMutablePointer<addrinfo>? = first
    while let current = node {
        if let address = current.pointee.ai_addr {
            if current.pointee.ai_family == AF_INET {
                let bytes = address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { pointer in withUnsafeBytes(of: pointer.pointee.sin_addr) { Array($0) } }
                if !publicAddress(bytes) { return true }
            } else if current.pointee.ai_family == AF_INET6 {
                let bytes = address.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) { pointer in withUnsafeBytes(of: pointer.pointee.sin6_addr) { Array($0) } }
                if !publicAddress(bytes) { return true }
            }
        }
        node = current.pointee.ai_next
    }
    return false
}


/// The public addresses a host's name resolves to on this Mac (IPv4 first, then IPv6), written out; empty when it has none or does not resolve.
func publicAddresses(of host: String) -> [String] {
    var hints = addrinfo()
    hints.ai_family = AF_UNSPEC
    hints.ai_socktype = SOCK_STREAM
    var result: UnsafeMutablePointer<addrinfo>?
    guard getaddrinfo(host, nil, &hints, &result) == 0, let first = result else { return [] }
    defer { freeaddrinfo(first) }
    var four: [String] = [], six: [String] = []
    var node: UnsafeMutablePointer<addrinfo>? = first
    while let current = node {
        if let address = current.pointee.ai_addr {
            if current.pointee.ai_family == AF_INET {
                var raw = address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
                var text = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                if publicAddress(withUnsafeBytes(of: &raw) { Array($0) }), inet_ntop(AF_INET, &raw, &text, socklen_t(text.count)) != nil, !four.contains(String(cString: text)) { four.append(String(cString: text)) }
            } else if current.pointee.ai_family == AF_INET6 {
                var raw = address.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) { $0.pointee.sin6_addr }
                var text = [CChar](repeating: 0, count: Int(INET6_ADDRSTRLEN))
                if publicAddress(withUnsafeBytes(of: &raw) { Array($0) }), inet_ntop(AF_INET6, &raw, &text, socklen_t(text.count)) != nil, !six.contains(String(cString: text)) { six.append(String(cString: text)) }
            }
        }
        node = current.pointee.ai_next
    }
    return four + six
}

// MARK: Looking pages up through another resolver

/// A tunnel on 127.0.0.1, inside this process, that WebKit sends every connection through (an HTTP CONNECT proxy). It looks each host up through the
/// DNS-over-HTTPS resolver given (or, with none, through this Mac's own resolver) and connects straight to a public address it answers: bytes pass through unread,
/// TLS stays end to end between WebKit and the site. A connection is only ever made to an address that was checked here, so a name that answers differently a
/// second time (DNS rebinding) or a page that reaches for a local address cannot get past it.
final class ResolvingTunnel {
    let resolver: URL?  // nil: this Mac's resolver
    let listener: NWListener
    var answers: [String: [String]] = [:]
    var failures: [String: String] = [:]  // host: why the resolver gave no usable address

    init?(resolver: URL?) {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: .any)
        guard let listener = try? NWListener(using: parameters) else { return nil }
        self.resolver = resolver
        self.listener = listener
    }

    /// Start listening; ``ready`` gets the port, or nil if it could not listen.
    func start(_ ready: @escaping (UInt16?) -> Void) {
        var told = false
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready where !told: told = true; ready(self.listener.port?.rawValue)
            case .failed where !told: told = true; ready(nil)
            default: break
            }
        }
        listener.newConnectionHandler = { self.accept($0) }
        listener.start(queue: .main)
    }

    func accept(_ client: NWConnection) {
        client.start(queue: .main)
        readHead(client, Data())
    }

    func readHead(_ client: NWConnection, _ sofar: Data) {
        client.receive(minimumIncompleteLength: 1, maximumLength: 16384) { data, _, complete, error in
            var head = sofar
            if let data = data { head.append(data) }
            guard let end = head.range(of: Data("\r\n\r\n".utf8)) else {
                if complete || error != nil || head.count > 16384 { client.cancel() } else { self.readHead(client, head) }
                return
            }
            let line = String(decoding: head[..<end.lowerBound], as: UTF8.self).split(separator: "\r\n").first.map(String.init) ?? ""
            let parts = line.split(separator: " ")
            guard parts.count >= 2, parts[0] == "CONNECT", let colon = parts[1].lastIndex(of: ":"),
                  let port = UInt16(parts[1][parts[1].index(after: colon)...]) else {
                return self.refuse(client, "405 Method Not Allowed")
            }
            let host = String(parts[1][..<colon]).trimmingCharacters(in: CharacterSet(charactersIn: "[]")).lowercased()
            let early = head[end.upperBound...]
            self.resolve(host) { addresses in
                guard !addresses.isEmpty else { return self.refuse(client, "502 Bad Gateway") }
                self.connect(client, to: addresses[...], port: port, early: Data(early))
            }
        }
    }

    func refuse(_ client: NWConnection, _ status: String) {
        client.send(content: Data("HTTP/1.1 \(status)\r\nContent-Length: 0\r\n\r\n".utf8), completion: .contentProcessed { _ in client.cancel() })
    }

    /// Connect to the first of the addresses that answers; the rest are tried only if it cannot be reached. An address with no route leaves a
    /// connection waiting rather than failed (an IPv6 address on a network without IPv6), so waiting, or no answer within CONNECT_WAIT_S,
    /// counts as unreachable too: the next address is tried, and with none left the page fails at once instead of at the read's 30 s limit.
    static let CONNECT_WAIT_S = 10.0
    func connect(_ client: NWConnection, to addresses: ArraySlice<String>, port: UInt16, early: Data) {
        guard let address = addresses.first, let site = NWEndpoint.Port(rawValue: port) else { return refuse(client, "502 Bad Gateway") }
        let server = NWConnection(host: NWEndpoint.Host(address), port: site, using: .tcp)
        var settled = false  // this attempt either opened or gave up; later states of it change nothing
        var opened = false
        func giveUp() {
            guard !settled else { return }
            settled = true
            server.stateUpdateHandler = nil
            server.cancel()
            if addresses.count > 1 { connect(client, to: addresses.dropFirst(), port: port, early: early) } else { refuse(client, "502 Bad Gateway") }
        }
        server.stateUpdateHandler = { state in
            switch state {
            case .ready where !settled:
                settled = true
                opened = true
                client.send(content: Data("HTTP/1.1 200 Connection Established\r\n\r\n".utf8), completion: .contentProcessed { _ in
                    if !early.isEmpty { server.send(content: early, completion: .idempotent) }
                    self.pipe(client, server)
                    self.pipe(server, client)
                })
            case .waiting:
                giveUp()
            case .failed, .cancelled:
                if opened { client.cancel() } else { giveUp() }
            default: break
            }
        }
        server.start(queue: .main)
        DispatchQueue.main.asyncAfter(deadline: .now() + ResolvingTunnel.CONNECT_WAIT_S) { if !settled { giveUp() } }
    }

    func pipe(_ from: NWConnection, _ to: NWConnection) {
        from.receive(minimumIncompleteLength: 1, maximumLength: 65536) { data, _, complete, error in
            if let data = data, !data.isEmpty {
                to.send(content: data, completion: .contentProcessed { _ in
                    if complete || error != nil { to.send(content: nil, contentContext: .finalMessage, isComplete: true, completion: .idempotent) } else { self.pipe(from, to) }
                })
            } else if complete || error != nil {
                to.send(content: nil, contentContext: .finalMessage, isComplete: true, completion: .idempotent)
            } else {
                self.pipe(from, to)
            }
        }
    }

    /// One host's public addresses (IPv4 first, then IPv6), or none and the reason kept in ``failures``.
    func resolve(_ host: String, _ done: @escaping ([String]) -> Void) {
        if ipBytes(host) != nil { return done(isPublic(host) ? [host] : []) }
        if let known = answers[host] { return done(known) }
        guard resolver != nil else {
            DispatchQueue.global().async {
                let found = publicAddresses(of: host)
                DispatchQueue.main.async {
                    if found.isEmpty { self.failures[host] = "A: " + (resolvesToPrivate(host) ? "only a private or local address" : "no address") }
                    else { self.answers[host] = found }
                    done(found)
                }
            }
            return
        }
        lookup(host, type: "A") { first in
            if let first = first { self.answers[host] = [first]; return done([first]) }
            self.lookup(host, type: "AAAA") { second in
                if let second = second { self.answers[host] = [second] }
                done(second.map { [$0] } ?? [])
            }
        }
    }

    func lookup(_ host: String, type: String, _ done: @escaping (String?) -> Void) {
        guard let resolver = resolver else { return done(nil) }
        var parts = URLComponents(url: resolver, resolvingAgainstBaseURL: false)
        parts?.queryItems = [URLQueryItem(name: "name", value: host), URLQueryItem(name: "type", value: type)]
        guard let address = parts?.url else { return done(nil) }
        var request = URLRequest(url: address, timeoutInterval: 8)
        request.setValue("application/dns-json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: request) { data, response, error in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            let answer = (data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any])?["Answer"] as? [[String: Any]] ?? []
            let wanted = type == "A" ? 1 : 28
            let found = answer.first { ($0["type"] as? Int) == wanted && ($0["data"] as? String).map(self.isPublic) == true }?["data"] as? String
            DispatchQueue.main.async {
                if found == nil {
                    let why = error.map { $0.localizedDescription } ?? (code != 200 ? "HTTP \(code)" : answer.isEmpty ? "no address" : "only a private or local address")
                    let note = "\(type): \(why)"
                    if !(self.failures[host] ?? "").contains(note) { self.failures[host] = (self.failures[host].map { $0 + "; " } ?? "") + note }
                }
                done(found)
            }
        }.resume()
    }

    /// Whether an address, written out, is on the public internet (see ``publicAddress``).
    func isPublic(_ address: String) -> Bool {
        guard IPv4Address(address) != nil || IPv6Address(address) != nil, let bytes = ipBytes(address) else { return false }
        let multicast = bytes.count == 4 ? bytes[0] >= 224 : bytes[0] == 0xFF  // nothing to connect to there; the tunnel refuses it, the address check does not
        return publicAddress(bytes) && !multicast
    }
}

var tunnel: ResolvingTunnel?  // kept for the life of the process

/// Send a web view's connections through the tunnel, then go on. With ``GROUNDER_DIRECT_RESOLVER`` set (Grounder's, when the user picks it) the tunnel looks hosts
/// up there; otherwise, when ``always`` (a page read, not a sign-in window the user drives), through this Mac's own resolver, so that every connection goes only to
/// a public address that was checked. On macOS 13, which has no proxy setting for a web view, a read without a resolver has no tunnel, and the navigation checks
/// of ``Reader`` are what stand. Nil means it could not, and why is printed.
func useResolver(_ store: WKWebsiteDataStore, always: Bool = false, then next: @escaping (String?) -> Void) {
    let raw = ProcessInfo.processInfo.environment["GROUNDER_DIRECT_RESOLVER"] ?? ""
    var resolver: URL?
    if !raw.isEmpty {
        guard let given = URL(string: raw), given.scheme == "https" else { return next("the resolver address is not an https one: \(raw)") }
        resolver = given
    } else if !always {
        return next(nil)
    }
    guard #available(macOS 14.0, *) else { return next(raw.isEmpty ? nil : "using another resolver needs macOS 14 or later") }
    guard let made = ResolvingTunnel(resolver: resolver) else { return next("could not open the resolver tunnel") }
    tunnel = made
    made.start { port in
        guard let port = port, let local = NWEndpoint.Port(rawValue: port) else { return next("could not open the resolver tunnel") }
        store.proxyConfigurations = [ProxyConfiguration(httpCONNECTProxy: .hostPort(host: .ipv4(.loopback), port: local))]
        next(nil)
    }
}

// MARK: Read one page

let NOT_PUBLIC = "the address, or an address the page sent the browser to, is not a public web address (a private, loopback or link-local one, or a name that points at one), so nothing was read from it"

final class Reader: NSObject, WKNavigationDelegate {
    let web: WKWebView
    let maxChars: Int
    var status = 0
    var settle: DispatchWorkItem?
    var done = false
    var committed = false  // the main page has answered and begun to show
    var tries = 0  // reads of a page that showed no text yet
    var leftFor: String?  // the other site a shown page tried to send the browser to
    var requested: URL?  // the address asked for
    var site: String?  // the site whose saved sign-in this read carries
    var sent: [[String: Any]] = []
    var lookedUp: [String: Bool] = [:]  // host: whether this Mac's resolver gave only public addresses for it

    init(userAgent: String, maxChars: Int) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 900), configuration: config)
        web.customUserAgent = userAgent
        self.maxChars = maxChars
        super.init()
        web.navigationDelegate = self
    }

    func emit(_ object: [String: Any]) {
        if done { return }
        done = true
        printLine(object)
        exit(0)
    }

    /// Set the saved cookies in this read's empty session, then go on.
    func setCookies(_ cookies: [HTTPCookie], then next: @escaping () -> Void) {
        let store = web.configuration.websiteDataStore.httpCookieStore
        let group = DispatchGroup()
        for cookie in cookies {
            group.enter()
            store.setCookie(cookie) { group.leave() }
        }
        group.notify(queue: .main, execute: next)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if response.isForMainFrame, let http = response.response as? HTTPURLResponse {
            status = http.statusCode
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        settle?.cancel()
        let work = DispatchWorkItem { self.scrollThenRead() }
        settle = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5, execute: work)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) { committed = true }

    /// Whether a navigation may go ahead: an http(s) address whose host, as WebKit itself reads it, is a public one (``privateHost``, and, with no tunnel to pin the
    /// connection, what this Mac's resolver answers for it). Every kind of navigation passes through here: the address asked for, a redirect, a script's, a meta
    /// refresh, a frame's. ``about:`` pages are the browser's own and always pass; any other scheme may not load in the main frame.
    func allowed(_ url: URL?, mainFrame: Bool, _ answer: @escaping (Bool) -> Void) {
        guard let url = url, let scheme = url.scheme?.lowercased() else { return answer(false) }
        if scheme == "about" { return answer(true) }
        guard scheme == "http" || scheme == "https" else { return answer(!mainFrame) }
        guard let host = url.host, !privateHost(host) else { return answer(false) }
        if tunnel != nil || ipBytes(host.lowercased()) != nil { return answer(true) }
        if let known = lookedUp[host.lowercased()] { return answer(known) }
        DispatchQueue.global().async {
            let ok = !resolvesToPrivate(host)
            DispatchQueue.main.async { self.lookedUp[host.lowercased()] = ok; answer(ok) }
        }
    }

    // A page that, once shown, sends the browser to another site's address: remembered, so a page that then has no text can say where it went.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if committed, action.targetFrame?.isMainFrame == true, let to = action.request.url?.host?.lowercased(),
           let from = webView.url?.host?.lowercased(), to != from, !to.hasSuffix("." + from), !from.hasSuffix("." + to) {
            leftFor = to
        }
        let mainFrame = action.targetFrame?.isMainFrame ?? true
        allowed(action.request.url, mainFrame: mainFrame) { ok in
            if ok { return decisionHandler(.allow) }
            decisionHandler(.cancel)
            // Before the page has answered, the refused address is where the read was going: say so. Later, the page stays as it is and is read.
            if mainFrame && !self.committed { self.emit(["error": NOT_PUBLIC, "status": 0]) }
        }
    }

    // A navigation that fails after the page has answered (a script's later redirect to a bad address, say: people.com, 2026-11-19) leaves that page in place,
    // so it is read; only a failure before any answer is an error. A navigation cancelled because a newer one replaced it is never one.
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func failed(_ error: Error) {
        let webView = web
        if (error as NSError).code == NSURLErrorCancelled { return }
        if !committed && status == 0 {
            // The address that failed, which after a redirect is not the one asked for.
            let failedHost = ((error as NSError).userInfo[NSURLErrorFailingURLErrorKey] as? URL)?.host?.lowercased() ?? (webView.url ?? requested)?.host?.lowercased()
            if let host = failedHost, let why = tunnel?.failures[host], tunnel?.resolver == nil {
                if why.contains("private") { return emit(["error": NOT_PUBLIC, "status": 0]) }
            } else if let host = failedHost, let why = tunnel?.failures[host] {
                return emit(["error": "the resolver you picked did not answer for \(host) (\(why)); /search settings can switch back to your own resolver",
                             "status": 0, "resolver": "grounder"])
            }
            return emit(["error": "load failed: \(error.localizedDescription)", "status": status])
        }
        if settle == nil {
            let work = DispatchWorkItem { self.scrollThenRead() }
            settle = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5, execute: work)
        }
    }

    // Scroll as a reader would, so what a page loads on demand gets its chance.
    func scrollThenRead() {
        web.evaluateJavaScript("window.scrollTo(0, document.body.scrollHeight)") { _, _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                self.web.evaluateJavaScript("window.scrollTo(0, document.body.scrollHeight)") { _, _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.read() }
                }
            }
        }
    }

    func read() {
        let js = """
        JSON.stringify((() => {
          const text = document.body ? document.body.innerText : '';
          const seen = new Set(), links = [];
          for (const a of document.querySelectorAll('a[href]')) {
            const url = a.href.split('#')[0];
            const title = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
            if (!/^https?:/.test(url) || !title || seen.has(url)) continue;
            seen.add(url);
            links.push({title: title.slice(0, 300), url});
            if (links.length >= 400) break;
          }
          return {url: location.href, title: document.title, text: text.slice(0, \(maxChars)), chars: text.length, links};
        })())
        """
        web.evaluateJavaScript(js) { value, error in
            guard let raw = value as? String, let data = raw.data(using: .utf8),
                  var page = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                self.emit(["error": "could not read the page: \(String(describing: error))", "status": self.status])
                return
            }
            page["status"] = self.status
            // A page still drawing itself can have no text yet: look again, up to three more times, before calling it empty.
            if (page["chars"] as? Int ?? 0) == 0, let away = self.leftFor {
                // Typically a site's ad-blocker check: its script did not load, so it took its page down and sent the browser away. Nothing is done to get past it.
                page["error"] = "the site took its page down and tried to send the browser to \(away) instead. That is usually the site's own ad-blocker check, "
                    + "set off when one of its scripts cannot load, for example because an ad-blocking filter on this Mac or its network blocks it"
                return self.finish(page)
            }
            if (page["chars"] as? Int ?? 0) == 0, self.tries < 3 {
                self.tries += 1
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.read() }
                return
            }
            self.finish(page)
        }
    }

    /// Say which saved sign-in the read carried (the site and how many cookies, never their values), and save back what the site changed.
    func finish(_ page: [String: Any]) {
        var page = page
        if tunnel?.resolver != nil { page["resolver"] = "grounder" }
        guard let site = site else { return emit(page) }
        page["signed_in"] = site
        page["cookies_sent"] = sent.count
        web.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
            let now = widened(cookies.filter { belongs($0.domain, to: site) }.map(cookieRecord), to: site)  // the per-host copies fold back into one
            if self.status < 400, !now.isEmpty, fingerprint(now) != fingerprint(widened(self.sent, to: site)) {
                if let why = saveSignIn(site: site, records: now, how: "refresh") {
                    page["sign_in_not_refreshed"] = why
                } else {
                    page["sign_in_refreshed"] = true
                }
            }
            self.emit(page)
        }
    }
}

// MARK: Sign in to a site in a window

final class SignIn: NSObject, NSWindowDelegate {
    let site: String
    let window: NSWindow
    let web: WKWebView
    var finished = false

    init(site: String, userAgent: String, start: URL) {
        self.site = site
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: .zero, configuration: config)
        web.customUserAgent = userAgent  // the user agent every read of the site carries, so the site sees one browser
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 800), styleMask: [.titled, .closable, .resizable, .miniaturizable],
                          backing: .buffered, defer: false)
        super.init()
        window.title = "Grounder direct: sign in to \(site)"
        window.delegate = self
        window.isReleasedWhenClosed = false

        let note = NSTextField(wrappingLabelWithString: "Sign in to \(site) above, then click Save sign-in. Its cookies for \(site) are sealed on this Mac under a key in your "
                               + "login keychain, and sent only to \(site), when Grounder direct reads one of its pages. Cancel or closing this window saves nothing.")
        note.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        note.textColor = .secondaryLabelColor
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancelPressed))
        let save = NSButton(title: "Save sign-in", target: self, action: #selector(savePressed))
        save.keyEquivalent = "s"
        save.keyEquivalentModifierMask = .command
        save.bezelColor = .controlAccentColor
        let bar = NSStackView(views: [note, cancel, save])
        bar.orientation = .horizontal
        bar.alignment = .centerY
        bar.spacing = 12
        bar.edgeInsets = NSEdgeInsets(top: 10, left: 16, bottom: 10, right: 16)
        note.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        cancel.setContentHuggingPriority(.required, for: .horizontal)
        save.setContentHuggingPriority(.required, for: .horizontal)

        let column = NSStackView(views: [web, bar])
        column.orientation = .vertical
        column.spacing = 0
        column.setHuggingPriority(.defaultLow, for: .vertical)
        web.setContentHuggingPriority(.defaultLow, for: .vertical)
        window.contentView = column
        window.center()
    }

    /// Show the window, with the sign-in already saved for the site set in it, so it opens as the site last saw the user.
    func show(start: URL, existing: [HTTPCookie]) {
        let store = web.configuration.websiteDataStore.httpCookieStore
        let group = DispatchGroup()
        for cookie in existing {
            group.enter()
            store.setCookie(cookie) { group.leave() }
        }
        group.notify(queue: .main) {
            self.web.load(URLRequest(url: start))
            self.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    @objc func cancelPressed() { window.close() }

    @objc func savePressed() {
        web.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
            let records = cookies.filter { belongs($0.domain, to: self.site) }.map(cookieRecord)
            if records.isEmpty {
                let alert = NSAlert()
                alert.messageText = "No cookies for \(self.site) yet"
                alert.informativeText = "Sign in to \(self.site) in this window first, or click Cancel."
                alert.beginSheetModal(for: self.window)
                return
            }
            if let why = saveSignIn(site: self.site, records: records, how: "signin") {
                let alert = NSAlert()
                alert.messageText = "Could not save the sign-in"
                alert.informativeText = why
                alert.beginSheetModal(for: self.window)
                return
            }
            self.finished = true
            printLine(["site": self.site, "saved": records.count, "names": records.compactMap { $0["name"] as? String }.sorted()])
            exit(0)
        }
    }

    func windowWillClose(_ notification: Notification) {
        if !finished {
            printLine(["site": site, "canceled": true])
            exit(0)
        }
    }
}

/// An app menu (Quit) and an Edit menu, so a password can be pasted into the site's form with Command-V.
func installMenus() {
    let main = NSMenu()
    let appItem = NSMenuItem()
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu
    main.addItem(appItem)
    let editItem = NSMenuItem()
    let edit = NSMenu(title: "Edit")
    edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
    edit.addItem(.separator())
    edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    editItem.submenu = edit
    main.addItem(editItem)
    NSApp.mainMenu = main
}

// MARK: Main

func validSite(_ raw: String) -> String? {
    let site = raw.lowercased()
    return site.range(of: "^[a-z0-9-]+(\\.[a-z0-9-]+)+$", options: .regularExpression) != nil ? site : nil
}

/// The reader itself. ``args`` is a command line: ``args[0]`` the program, then ``<url> <user agent> [max]``, ``--signin ...`` or ``--paste ...``. Built alone
/// (the Hermes plugin's fallback) the file calls it with this process's own arguments; built into grounder-direct that binary's
/// ``reader`` subcommand does.
func readerMain(_ args: [String]) -> Never {
let app = NSApplication.shared

if args.count >= 2, args[1] == "--paste" {
    // A Cookie header (``name=value; other=value``) for a site, from standard input: for a site that will not sign in inside this window.
    guard args.count >= 3, let site = validSite(args[2]) else {
        printLine(["error": "usage: direct-reader --paste <site>"])
        exit(64)
    }
    let typed = isatty(STDIN_FILENO) == 1
        ? hiddenLine("Cookie header for \(site) (hidden; from Safari's Web Inspector, Network, a request to \(site), its Cookie header): ")
        : String(decoding: FileHandle.standardInput.readDataToEndOfFile(), as: UTF8.self)
    guard let typed = typed else {
        printLine(["error": "nothing was entered, so nothing was saved"])
        exit(65)
    }
    let header = typed.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "^[Cc]ookie:\\s*", with: "", options: .regularExpression)
    var records: [[String: Any]] = []
    for part in header.split(separator: ";") {
        let pair = part.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
        guard pair.count == 2, !pair[0].isEmpty else { continue }
        records.append(["name": pair[0], "value": pair[1], "domain": "." + site, "path": "/", "secure": true, "httpOnly": true])
    }
    if records.isEmpty {
        printLine(["error": "no cookies in what was pasted; paste the request's Cookie header, name=value pairs split by semicolons"])
        exit(65)
    }
    if let why = saveSignIn(site: site, records: records, how: "paste") {
        printLine(["error": why])
        exit(70)
    }
    printLine(["site": site, "saved": records.count, "names": records.compactMap { $0["name"] as? String }.sorted()])
    exit(0)
}

if args.count >= 2, args[1] == "--signin" {
    guard args.count >= 4, let site = validSite(args[2]) else {
        printLine(["error": "usage: direct-reader --signin <site> <user agent> [start address]"])
        exit(64)
    }
    let start = args.count > 4 ? URL(string: args[4]) : URL(string: "https://\(site)/")
    guard let start = start, start.scheme == "https" else {
        printLine(["error": "the start address must be an https address"])
        exit(64)
    }
    var existing: [HTTPCookie] = []
    switch savedSignIn(for: site) {
    case .found(let saved, let cookies, _) where saved == site: existing = cookies
    case .failed(let why):
        printLine(["error": why])
        exit(70)
    default: break
    }
    app.setActivationPolicy(.regular)
    installMenus()
    let signIn = SignIn(site: site, userAgent: args[3], start: start)
    useResolver(signIn.web.configuration.websiteDataStore) { problem in
        if let problem = problem {
            printLine(["error": problem])
            exit(70)
        }
        signIn.show(start: start, existing: existing)
    }
    app.run()
}

guard args.count >= 3, let url = URL(string: args[1]) else {
    printLine(["error": "usage: direct-reader <url> <user agent> [max chars]"])
    exit(64)
}
app.setActivationPolicy(.prohibited)
let reader = Reader(userAgent: args[2], maxChars: args.count > 3 ? Int(args[3]) ?? 60000 : 60000)
var request = URLRequest(url: url)
request.setValue("1", forHTTPHeaderField: "Sec-GPC")  // Safari sends Global Privacy Control on every request; WebKit alone does not
reader.requested = url
useResolver(reader.web.configuration.websiteDataStore, always: true) { problem in
    if let problem = problem {
        printLine(["error": problem, "status": 0])
        exit(0)
    }
    // GROUNDER_DIRECT_SIGNED_OUT=1: a logged-out read, asked for or after a sign-in that could not be used. No saved sign-in is looked up, set or saved back.
    let signedOut = ProcessInfo.processInfo.environment["GROUNDER_DIRECT_SIGNED_OUT"] == "1"
    switch signedOut ? .none : savedSignIn(for: url.host ?? "") {
    case .failed(let why):
        printLine(["error": why, "status": 0])
        exit(0)
    case .found(let site, let cookies, let records):
        reader.site = site
        reader.sent = records.filter { record in cookies.contains { $0.name == record["name"] as? String && $0.domain == record["domain"] as? String } }
        reader.setCookies(forHosts(cookies, site: site, host: url.host ?? site)) { reader.web.load(request) }
    case .none:
        reader.web.load(request)
    }
}
DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
    reader.emit(["error": "timed out after 30 s", "status": reader.status])
}
app.run()
exit(0)
}

// Built alone (the Hermes plugin's fallback, ``swiftc direct_reader.swift``), this file is the program, and its globals run in order: this one starts the
// reader. Built with other files into grounder-direct, it is not the main file, its globals are lazy and nothing touches this one: that binary's
// ``reader`` subcommand calls ``readerMain`` itself.
let standaloneReader: Void = { readerMain(CommandLine.arguments) }()
