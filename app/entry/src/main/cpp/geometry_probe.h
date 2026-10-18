#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <memory>
#include "geometry_lease.h"
#include "geometry_socket.h"
#include <multimedia/player_framework/native_avscreen_capture.h>

// Explicit PC-requested geometry diagnostics only. No historical frame queue,
// no pen protocol changes, and no attempt to authorize or start screen capture.
class GeometryProbe {
public:
    explicit GeometryProbe(const std::string &directory);
    ~GeometryProbe();
    bool Enabled() const { return enabled; }
    int Width() const { return width; }
    int Height() const { return height; }
    void Frame(OH_AVBuffer *buffer, int64_t timestamp);
    void FrameRateResult(int32_t code) { frameRateResult.store(code); }
    void Stop() { stopping.store(true); if (socket) socket->Stop(); }

private:
    bool enabled = false;
    int width = 320, height = 208;
    int reduction = 1;
    int64_t expires = 0, lastCheck = 0, lastLeaseCheck = 0;
    GeometryLease lease;
    std::unique_ptr<GeometrySocket> socket;
    std::string directory;
    std::mutex callbackMutex;
    std::atomic<bool> busy{false}, stopping{false};
    std::atomic<int64_t> publishedAt{0};
    // Count every delivered video callback, including frames not requested by
    // the PC. A requested-frame interval is not the screen stream frame rate.
    std::atomic<uint64_t> videoCallbacks{0};
    std::atomic<int32_t> frameRateResult{-1}; // -1: API not attempted yet.
    std::thread worker;
};
