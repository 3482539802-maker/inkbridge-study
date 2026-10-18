#include "capture_clock.h"
#include <chrono>
#include <iomanip>
#include <sstream>
#include <utility>

namespace {
void Reading(std::ostream &out, const CaptureClockReading &r)
{
    out << "{\"monotonic_before_us\":" << r.before << ",\"monotonic_after_us\":" << r.after
        << ",\"boottime_us\":" << r.boot << ",\"unix_ms\":" << r.unixMs << '}';
}
}

CaptureClockHistory::CaptureClockHistory(std::string path, std::string session, Writer writer)
    : path(std::move(path)), session(std::move(session)), writer(std::move(writer)) {}

void CaptureClockHistory::Anchor(CaptureClockReading reading)
{
    anchors.push_back(reading);
    ++anchorsSince;
    if (anchors.size() > 32) {
        if (anchors.front().before > committedAnchor) lost = true;
        anchors.pop_front();
    }
}

void CaptureClockHistory::Observe(CaptureClockObservation observation)
{
    observations.push_back(observation);
    ++observationsSince;
    if (observations.size() > 32) {
        if (observations.front().seq > committedSequence) lost = true;
        observations.pop_front();
    }
}

std::string CaptureClockHistory::Json(bool stopped) const
{
    std::ostringstream out;
    out << std::setprecision(17) << "{\"history_format\":1,\"history_segments\":" << segments
        << ",\"clock_producer\":\"native_input_and_worker_v1\",\"history_lost\":" << (lost ? "true" : "false")
        << ",\"stopped\":" << (stopped ? "true" : "false")
        << ",\"session_id\":\"" << session << "\",\"event_clock_verified\":false,\"anchors\":[";
    bool first = true;
    for (const auto &a : anchors) { if (!first) out << ','; Reading(out, a); first = false; }
    out << "],\"observations\":[";
    first = true;
    for (const auto &o : observations) {
        if (!first) out << ',';
        out << "{\"seq\":" << o.seq << ",\"action_time_us\":" << o.action
            << ",\"sampling_stage\":\"native_input_callback\",\"callback_clock\":";
        Reading(out, o.receipt); out << '}'; first = false;
    }
    out << "]}";
    return out.str();
}

bool CaptureClockHistory::Publish(bool final)
{
    // Publish progress only after an immutable segment is durable. Failure
    // never advances its index; ring eviction without a checkpoint is explicit.
    if (final || anchorsSince >= 16 || observationsSince >= 16 || !pendingCheckpoint.empty()) {
        if (pendingCheckpoint.empty()) {
            pendingCheckpoint = Json();
            pendingAnchors = anchorsSince; pendingObservations = observationsSince;
            pendingAnchor = anchors.empty() ? -1 : anchors.back().before;
            pendingSequence = observations.empty() ? 0 : observations.back().seq;
        }
        if (!writer(path + ".clock-" + std::to_string(segments) + ".json", pendingCheckpoint)) return false;
        ++segments;
        committedAnchor = pendingAnchor; committedSequence = pendingSequence;
        anchorsSince -= pendingAnchors; observationsSince -= pendingObservations;
        pendingCheckpoint.clear();
        // A recovered pending segment may predate the current stop tail. Store
        // that tail too, rather than merely replacing the volatile latest.
        if (final && (anchorsSince || observationsSince)) return Publish(true);
    }
    return writer(path + ".clock.json", Json(final));
}

CaptureClockRecorder::CaptureClockRecorder(std::string path, std::string session,
    Sampler sampler, CaptureClockHistory::Writer writer)
    : sampler(std::move(sampler)), history(std::move(path), std::move(session), std::move(writer))
{
    history.Anchor(this->sampler());
    history.Publish();
    worker = std::thread([this] { Run(); });
}

CaptureClockRecorder::~CaptureClockRecorder()
{
    { std::lock_guard<std::mutex> guard(mutex); closing = true; }
    wake.notify_one();
    if (worker.joinable()) worker.join();
}

void CaptureClockRecorder::Observe(uint64_t seq, int64_t action, CaptureClockReading receipt)
{
    std::lock_guard<std::mutex> guard(mutex);
    if (closing || receipt.before < nextObservation) return;
    nextObservation = receipt.before + 5000000;
    if (pending.size() >= 32) { overflow = true; return; }
    pending.push_back({seq, action, receipt});
    wake.notify_one();
}

void CaptureClockRecorder::Run()
{
    // Exceptions in optional metadata must not terminate the capture process.
    try {
        for (;;) {
            std::deque<CaptureClockObservation> work;
            bool finished, lost;
            {
                std::unique_lock<std::mutex> lock(mutex);
                wake.wait_for(lock, std::chrono::seconds(5), [this] { return closing || !pending.empty(); });
                work.swap(pending); finished = closing; lost = overflow; overflow = false;
            }
            if (lost) history.Lost();
            // Checkpoint during a backlog drain before either bounded ring can
            // discard uncommitted evidence; all timestamps are original receipt.
            for (const auto &o : work) {
                history.Observe(o);
                history.Anchor(sampler());
                history.Publish();
            }
            if (work.empty() || finished) { history.Anchor(sampler()); history.Publish(finished); }
            if (finished) break;
        }
    } catch (...) { /* Raw capture continues; missing/incomplete clock is rejected. */ }
}
