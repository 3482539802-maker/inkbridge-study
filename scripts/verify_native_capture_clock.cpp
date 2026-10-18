// Host execution of the production native history/worker; no tablet or images.
#include "capture_clock.h"
#include "geometry_pixels.h"
#include "geometry_lease.h"
#include <atomic>
#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <thread>

CaptureClockReading At(double us)
{
    return {us, us + 2, us + 90000000, 1700000000000.0 + (us + 1) / 1000};
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    GeometryLease lease;
    lease.token = std::string(32, 'a');
    lease.expires = 60000;
    lease.requests = 100;
    assert(!lease.Available(1000));
    assert(!lease.Renew(std::string(32, 'b'), 90000, 30000));
    assert(!lease.Renew(lease.token, 900000, 30000));
    assert(lease.Renew(lease.token, 90000, 30000));
    assert(lease.Available(30000) && lease.requests == 0);
    lease.requests = 50;
    assert(!lease.Renew(lease.token, 90000, 40000));
    assert(lease.requests == 50); // replay cannot reset the request budget
    assert(!lease.Available(90000));
    lease.token.clear();
    assert(!lease.Renew(std::string(32, 'a'), 150000, 90000));
    // Real production copy/reduction with padded stride and channel conversion.
    const uint8_t pixels[]={0,10,20,255,4,14,24,255,99,99,99,99,
                            8,18,28,255,12,22,32,255,99,99,99,99};
    std::vector<uint8_t> rgba;
    CopyGeometryPixels(pixels,2,2,12,false,2,rgba);
    assert((rgba==std::vector<uint8_t>{6,16,26,255}));
    CopyGeometryPixels(pixels,2,2,12,true,2,rgba);
    assert((rgba==std::vector<uint8_t>{26,16,6,255}));
    CopyGeometryPixels(pixels,2,2,12,false,1,rgba);
    assert(rgba.size()==16 && rgba[8]==8 && rgba[12]==12);
    const std::filesystem::path root(argv[1]);
    bool writes = true;
    auto writer = [&writes](const std::string &path, const std::string &text) {
        if (!writes) return false;
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        out << text;
        return static_cast<bool>(out);
    };
    const std::string session = "5494f517-426f-4157-aa29-eadb39f41d39";
    // Exercise actual production ring/checkpoint serialization over two hours.
    const auto folder = root / "two-hour";
    std::filesystem::create_directories(folder);
    CaptureClockHistory history((folder / "clock").string(), session, writer);
    history.Anchor(At(1000));
    assert(history.Publish());
    for (unsigned seq = 1; seq <= 1440; ++seq) {
        const double us = 1000 + seq * 5001000.0;
        history.Observe({seq, static_cast<int64_t>(us - 1000), At(us)});
        history.Anchor(At(us + 100));
        assert(history.Publish());
    }
    assert(history.Publish(true));

    const auto failed = root / "failed";
    std::filesystem::create_directories(failed);
    CaptureClockHistory lossy((failed / "clock").string(), session, writer);
    writes = false;
    for (unsigned seq = 1; seq <= 50; ++seq) {
        lossy.Observe({seq, static_cast<int64_t>(seq) * 5001000, At(seq * 5001000.0 + 1000)});
        lossy.Anchor(At(seq * 5001000.0 + 2000));
        assert(!lossy.Publish());
    }
    writes = true;
    assert(lossy.Publish(true)); // Irrecoverable eviction must remain explicit.

    // Retry identical bytes after a rename reached disk but its acknowledgement
    // failed; never rewrite that immutable segment with a later window.
    const auto retry = root / "retry";
    std::filesystem::create_directories(retry);
    bool failAck = true;
    auto acknowledgedWriter = [&](const std::string &path, const std::string &text) {
        if (std::filesystem::exists(path) && path.find(".clock-") != std::string::npos) {
            std::ifstream input(path);
            std::string saved((std::istreambuf_iterator<char>(input)),{});
            assert(saved == text);
        }
        if (!writer(path,text)) return false;
        if (path.find(".clock-") != std::string::npos && failAck) {failAck = false; return false;}
        return true;
    };
    CaptureClockHistory retrying((retry/"clock").string(), session, acknowledgedWriter);
    retrying.Anchor(At(1000));
    for (unsigned seq = 1; seq <= 17; ++seq) {
        retrying.Observe({seq, static_cast<int64_t>(seq)*5001000, At(seq*5001000.0+1000)});
        retrying.Anchor(At(seq*5001000.0+2000));
        const bool saved = retrying.Publish();
        assert(seq == 15 ? !saved : saved);
    }
    assert(retrying.Publish(true));

    // The actual worker runs while no ArkTS timers or callbacks are executed.
    const auto worker = root / "worker";
    std::filesystem::create_directories(worker);
    std::atomic<double> now{1000};
    {
        CaptureClockRecorder recorder((worker / "clock").string(), session,
            [&now] { return At(now.load() + 100); }, writer);
        for (unsigned seq = 1; seq <= 8; ++seq) {
            now.store(1000 + seq * 5001000.0);
            recorder.Observe(seq, static_cast<int64_t>(now.load() - 1000), At(now.load()));
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        now.store(now.load() + 2000);
    } // Synchronous stop joins and commits the tail.
    std::cout << "Production native clock: 2h history, disk-failure eviction and independent worker/stop passed.\n";
}
