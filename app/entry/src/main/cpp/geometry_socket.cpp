#include "geometry_socket.h"
#include "geometry_lease.h"
#include <chrono>
#include <cstring>
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using Socket = SOCKET;
constexpr Socket BadSocket = INVALID_SOCKET;
static void Close(Socket s) { closesocket(s); }
#else
#include <arpa/inet.h>
#include <poll.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
using Socket = int;
constexpr Socket BadSocket = -1;
static void Close(Socket s) { close(s); }
#endif

namespace {
using Clock = std::chrono::steady_clock;
constexpr size_t MaxPacket = 8 * 1024 * 1024 + 8192;
// Poll in short slices so shutdown never waits on a dead peer or full socket.
bool Wait(Socket s, bool write, Clock::time_point deadline, const std::atomic<bool> &stop)
{
    while (!stop.load() && Clock::now() < deadline) {
#ifdef _WIN32
        fd_set set;
        FD_ZERO(&set); FD_SET(s, &set);
        timeval timeout{0, 100000};
        int result = select(static_cast<int>(s + 1), write ? nullptr : &set,
                            write ? &set : nullptr, nullptr, &timeout);
        if (result > 0) return true;
        if (result < 0) return false;
#else
        pollfd fd{s, static_cast<short>(write ? POLLOUT : POLLIN), 0};
        int result = poll(&fd, 1, 100);
        if (result > 0) return !(fd.revents & (POLLERR | POLLNVAL));
        if (result < 0) return false;
#endif
    }
    return false;
}

bool Send(Socket s, const uint8_t *data, size_t length, const std::atomic<bool> &stop)
{
    auto deadline = Clock::now() + std::chrono::seconds(2);
    while (length && Wait(s, true, deadline, stop)) {
#ifdef _WIN32
        int sent = send(s, reinterpret_cast<const char *>(data), static_cast<int>(length), 0);
#else
        int sent = send(s, data, length, MSG_NOSIGNAL | MSG_DONTWAIT);
#endif
        if (sent <= 0) return false;
        data += sent; length -= sent;
    }
    return length == 0 && !stop.load();
}

bool ReadLine(Socket s, std::string &line, const std::atomic<bool> &stop)
{
    line.clear();
    auto deadline = Clock::now() + std::chrono::seconds(10);
    while (line.size() < 128 && Wait(s, false, deadline, stop)) {
        char c;
        if (recv(s, &c, 1, 0) != 1) return false;
        // Limit a fragmented/unfinished request to two seconds after byte one.
        if (line.empty()) deadline = Clock::now() + std::chrono::seconds(2);
        if (c == '\n') return true;
        if (c < 32 || c > 126) return false;
        line += c;
    }
    return false;
}
}

GeometrySocket::GeometrySocket(const std::string &key, uint16_t port) : token(key)
{
    if (!GeometryLease::ValidToken(token)) return;
    try { thread = std::thread([this, port] { Run(port); }); }
    catch (...) { stopping.store(true); }
}

GeometrySocket::~GeometrySocket()
{
    Stop();
    if (thread.joinable()) thread.join();
}

void GeometrySocket::Stop()
{
    stopping.store(true);
    ready.notify_all();
}

uint64_t GeometrySocket::Take(std::string &key)
{
    std::lock_guard<std::mutex> lock(mutex);
    if (stopping.load() || !pending || taken) return 0;
    key = nonce;
    taken = true;
    return pending;
}

void GeometrySocket::Publish(uint64_t request, const std::string &header, const std::vector<uint8_t> &jpeg)
{
    if (header.size() + jpeg.size() > MaxPacket) return;
    std::lock_guard<std::mutex> lock(mutex);
    if (stopping.load() || !request || pending != request || !taken || !response.empty()) return;
    response.assign(header.begin(), header.end());
    response.insert(response.end(), jpeg.begin(), jpeg.end());
    ready.notify_one();
}

void GeometrySocket::Run(uint16_t port)
{
#ifdef _WIN32
    WSADATA state;
    if (WSAStartup(MAKEWORD(2, 2), &state)) return;
#endif
    Socket listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener != BadSocket) {
        // Reuse only on POSIX. Windows SO_REUSEADDR can steal a live endpoint.
#ifndef _WIN32
        int reuse = 1;
        setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
#endif
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = htons(port);
        if (bind(listener, reinterpret_cast<sockaddr *>(&address), sizeof(address)) == 0 &&
            listen(listener, 1) == 0) {
#ifdef _WIN32
            int size = sizeof(address);
#else
            socklen_t size = sizeof(address);
#endif
            if (getsockname(listener, reinterpret_cast<sockaddr *>(&address), &size) == 0)
                boundPort.store(ntohs(address.sin_port));
            while (!stopping.load()) {
                if (!Wait(listener, false, Clock::now() + std::chrono::milliseconds(200), stopping)) continue;
                Socket client = accept(listener, nullptr, nullptr);
                if (client == BadSocket) continue;
#ifdef _WIN32
                u_long nonblocking = 1;
                ioctlsocket(client, FIONBIO, &nonblocking);
#endif
                try {
                    std::string line;
                    const std::string prefix = "INKGEOM1 " + token + " ";
                    while (!stopping.load() && ReadLine(client, line, stopping)) {
                        if (line.size() != prefix.size() + 32 || line.compare(0, prefix.size(), prefix) != 0 ||
                            !GeometryLease::ValidToken(line.substr(prefix.size()))) break;
                        std::vector<uint8_t> packet;
                        {
                            std::unique_lock<std::mutex> lock(mutex);
                            pending = ++sequence; nonce = line.substr(prefix.size()); taken = false;
                            ready.wait_for(lock, std::chrono::seconds(5), [this] {
                                return stopping.load() || !response.empty();
                            });
                            packet.swap(response); pending = 0; nonce.clear(); taken = false;
                        }
                        if (stopping.load() || packet.empty()) break;
                        uint32_t length = htonl(static_cast<uint32_t>(packet.size()));
                        if (!Send(client, reinterpret_cast<uint8_t *>(&length), sizeof(length), stopping) ||
                            !Send(client, packet.data(), packet.size(), stopping)) break;
                    }
                } catch (...) { /* Failure cannot escape the diagnostic worker. */ }
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    pending = 0; nonce.clear(); taken = false;
                    std::vector<uint8_t>().swap(response);
                }
                Close(client);
            }
        }
        Close(listener);
    }
    boundPort.store(0);
#ifdef _WIN32
    WSACleanup();
#endif
}
