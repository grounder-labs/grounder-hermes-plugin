// Copyright 2026 Grounder Labs, Inc
// SPDX-License-Identifier: Apache-2.0
//
// Grounder direct: read one page in a hidden WebKit view, asked for with the user agent given (Safari's own), and print it as one JSON line.
//
//   direct-reader <url> <user agent> [most characters of text]   read one page
//
// Every read is logged out: no saved sign-in, no cookie and no keychain item is read, set or saved (Grounder direct keeps none since 2026-11-27).
//
// With GROUNDER_DIRECT_RESOLVER set to a DNS-over-HTTPS address (Grounder's, when the user picks it), pages are looked up there instead of through this Mac's
// resolver, so a filter on the Mac or its network does not decide what loads. The pages themselves still go straight from this Mac to each site, encrypted end
// to end: the resolver learns which hosts are read, never what is on them. An answer that points at a private, loopback or link-local address is refused.
//
// Each read starts a fresh, empty session. Nothing is kept. A site's own checks run as in any browser; nothing here solves or skips one.

import AppKit
import Network
import WebKit

let SECURITY = "/usr/bin/security"
/// The home directory: ``HOME`` when it names one (so a test or a second profile can point it at its own folder), else the user's own.
func homeDirectory() -> URL {
    if let home = ProcessInfo.processInfo.environment["HOME"], home.hasPrefix("/") { return URL(fileURLWithPath: home, isDirectory: true) }
    return FileManager.default.homeDirectoryForCurrentUser
}
let NOT_FOUND: Int32 = 44  // errSecItemNotFound, as security exits

func printLine(_ object: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{\"error\":\"could not encode the result\"}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

// MARK: Running /usr/bin/security

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

    /// Say whether the read went through Grounder's resolver, and print the page.
    func finish(_ page: [String: Any]) {
        var page = page
        if tunnel?.resolver != nil { page["resolver"] = "grounder" }
        emit(page)
    }
}

// MARK: Main

/// The reader itself. ``args`` is a command line: ``args[0]`` the program, then ``<url> <user agent> [max]``. Built alone
/// (the Hermes plugin's fallback) the file calls it with this process's own arguments; built into grounder-direct that binary's
/// ``reader`` subcommand does.
func readerMain(_ args: [String]) -> Never {
let app = NSApplication.shared

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
    reader.web.load(request)
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
