#pragma once
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// A separate loopback-only, single-request geometry channel. It neither starts
// capture nor carries raw ink. The screen-session owner still checks its lease
// before taking a request. No frame history or disk response is retained here.
class GeometrySocket {
public:
    explicit GeometrySocket(const std::string &token, uint16_t port = 18769);
    ~GeometrySocket();
    void Stop();
    uint64_t Take(std::string &nonce);
    void Publish(uint64_t request, const std::string &header, const std::vector<uint8_t> &jpeg);
    uint16_t Port() const { return boundPort.load(); }
private:
    void Run(uint16_t port);
    const std::string token;
    std::atomic<bool> stopping{false};
    std::atomic<uint16_t> boundPort{0};
    std::thread thread;
    std::mutex mutex;
    std::condition_variable ready;
    uint64_t sequence = 0, pending = 0;
    bool taken = false;
    std::string nonce;
    std::vector<uint8_t> response;
};
