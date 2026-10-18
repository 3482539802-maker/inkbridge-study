#pragma once
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

struct CaptureClockReading {
    double before = 0, after = 0, boot = 0, unixMs = 0;
};
struct CaptureClockObservation {
    uint64_t seq;
    int64_t action;
    CaptureClockReading receipt;
};

// Platform-independent history logic; the same implementation is exercised by
// the host fault/recovery tests and linked into the tablet library.
class CaptureClockHistory {
public:
    using Writer = std::function<bool(const std::string &, const std::string &)>;
    CaptureClockHistory(std::string path, std::string session, Writer writer);
    void Anchor(CaptureClockReading reading);
    void Observe(CaptureClockObservation observation);
    void Lost() { lost = true; }
    bool Publish(bool final = false);
    std::string Json(bool stopped = false) const;
private:
    std::string path, session;
    Writer writer;
    std::deque<CaptureClockReading> anchors;
    std::deque<CaptureClockObservation> observations;
    unsigned segments = 0, anchorsSince = 0, observationsSince = 0;
    double committedAnchor = -1;
    uint64_t committedSequence = 0;
    bool lost = false;
    std::string pendingCheckpoint;
    unsigned pendingAnchors = 0, pendingObservations = 0;
    double pendingAnchor = -1;
    uint64_t pendingSequence = 0;
};

// Neither the sampler nor fsync depends on the ArkTS/UI event loop. Input only
// enqueues a throttled numeric reading; disk I/O never holds the input lock.
class CaptureClockRecorder {
public:
    using Sampler = std::function<CaptureClockReading()>;
    CaptureClockRecorder(std::string path, std::string session, Sampler sampler, CaptureClockHistory::Writer writer);
    ~CaptureClockRecorder();
    void Observe(uint64_t seq, int64_t action, CaptureClockReading receipt);
private:
    void Run();
    Sampler sampler;
    CaptureClockHistory history;
    std::mutex mutex;
    std::condition_variable wake;
    std::deque<CaptureClockObservation> pending;
    double nextObservation = 0;
    bool closing = false, overflow = false;
    std::thread worker;
};
