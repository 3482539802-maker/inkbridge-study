#include <atomic>
#include <cstdint>
#include <new>
#include <mutex>
#include <vector>
#include <time.h>
#include <memory>
#include <cerrno>
#include <fcntl.h>
#include <unistd.h>
#include "capture_clock.h"
#include <multimodalinput/oh_input_manager.h>
#include <napi/native_api.h>

void RegisterScreenSession(napi_env env, napi_value exports);

namespace {
struct Point {
    uint64_t seq;
    int32_t action, id, x, y, tool, window, display;
    int64_t time, down;
    double pressure;
};
std::atomic<bool> running{false};
std::atomic<uint64_t> count{0}, dropped{0};
struct CaptureSession {
    napi_threadsafe_function sink = nullptr;
    std::vector<napi_deferred> stops;
    int stopCode = 0;
};
std::mutex captureMutex;
CaptureSession *session = nullptr;
std::unique_ptr<CaptureClockRecorder> clockRecorder;

CaptureClockReading ReadCaptureClock()
{
    timespec before{}, wall{}, boot{}, after{};
    if (clock_gettime(CLOCK_MONOTONIC, &before) || clock_gettime(CLOCK_REALTIME, &wall) ||
        clock_gettime(CLOCK_BOOTTIME, &boot) || clock_gettime(CLOCK_MONOTONIC, &after)) return {};
    auto micros = [](const timespec &t) { return t.tv_sec * 1000000.0 + t.tv_nsec / 1000.0; };
    return {micros(before), micros(after), micros(boot), micros(wall) / 1000.0};
}

bool WriteClockFile(const std::string &path, const std::string &text)
{
    // Checkpoints are immutable; only the small latest sidecar is replaced.
    const bool checkpoint = path.find(".clock-") != std::string::npos;
    if (checkpoint && access(path.c_str(), F_OK) == 0) {
        // A directory fsync can fail after rename. An identical retry is
        // safe; conflicting bytes must never replace immutable evidence.
        int existing = open(path.c_str(), O_RDONLY);
        if (existing < 0) return false;
        std::string saved(text.size() + 1, '\0');
        size_t offset = 0;
        while (offset < saved.size()) {
            auto n = read(existing, saved.data() + offset, saved.size() - offset);
            if (n < 0 && errno == EINTR) continue;
            if (n < 0) { close(existing); return false; }
            if (n == 0) break;
            offset += static_cast<size_t>(n);
        }
        close(existing);
        saved.resize(offset);
        if (saved != text) return false;
        int directory = open(path.substr(0, path.rfind('/')).c_str(), O_RDONLY | O_DIRECTORY);
        if (directory < 0) return false;
        bool ok = fsync(directory) == 0;
        close(directory);
        return ok;
    }
    const auto temporary = path + ".tmp";
    int fd = open(temporary.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;
    size_t offset = 0;
    bool ok = true;
    while (offset < text.size()) {
        const auto n = write(fd, text.data() + offset, text.size() - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { ok = false; break; }
        offset += static_cast<size_t>(n);
    }
    if (ok) ok = fsync(fd) == 0;
    if (close(fd) != 0) ok = false;
    if (ok) ok = rename(temporary.c_str(), path.c_str()) == 0;
    if (!ok) { unlink(temporary.c_str()); return false; }
    // Persist the rename as well as the file before publishing segment count.
    int directory = open(path.substr(0, path.rfind('/')).c_str(), O_RDONLY | O_DIRECTORY);
    if (directory < 0) return false;
    ok = fsync(directory) == 0;
    close(directory);
    return ok;
}

void EndClock()
{
    std::unique_ptr<CaptureClockRecorder> completed;
    { std::lock_guard<std::mutex> guard(captureMutex); completed = std::move(clockRecorder); }
    // Join/fsync outside the input monitor lock.
    completed.reset();
}
void FinalizeSession(napi_env env, void *data, void *)
{
    auto completed = static_cast<CaptureSession *>(data);
    {
        std::lock_guard<std::mutex> guard(captureMutex);
        if (session == completed) session = nullptr;
    }
    // Finalization runs after the queue is drained, so callers can safely close the file.
    if (env) {
        napi_value result;
        napi_create_int32(env, completed->stopCode, &result);
        for (auto deferred : completed->stops) napi_resolve_deferred(env, deferred, result);
    }
    delete completed;
}

void Number(napi_env env, napi_value object, const char *key, double value)
{
    napi_value number;
    napi_create_double(env, value, &number);
    napi_set_named_property(env, object, key, number);
}

void Deliver(napi_env env, napi_value callback, void *, void *data)
{
    Point *point = static_cast<Point *>(data);
    if (env && callback && point) {
        napi_value object, type, receiver, ignored;
        napi_create_object(env, &object);
        napi_create_string_utf8(env, "point", NAPI_AUTO_LENGTH, &type);
        napi_set_named_property(env, object, "type", type);
        Number(env, object, "seq", point->seq);
        Number(env, object, "action", point->action);
        Number(env, object, "pointer_id", point->id);
        Number(env, object, "x", point->x);
        Number(env, object, "y", point->y);
        Number(env, object, "action_time", point->time);
        Number(env, object, "down_time", point->down);
        Number(env, object, "pressure", point->pressure);
        Number(env, object, "tool_type", point->tool);
        Number(env, object, "window_id", point->window);
        Number(env, object, "display_id", point->display);
        napi_get_undefined(env, &receiver);
        napi_call_function(env, receiver, callback, 1, &object, &ignored);
    }
    delete point;
}

void OnTouch(const Input_TouchEvent *event)
{
    // Receipt is sampled before the mutex/TSFN queue and ArkTS delivery.
    // It is optional numeric sidecar data, never an added raw journal field.
    const auto receipt = ReadCaptureClock();
    std::lock_guard<std::mutex> guard(captureMutex);
    if (!running.load() || !session || !event) return;
    auto point = new (std::nothrow) Point{
        ++count, OH_Input_GetTouchEventAction(event), OH_Input_GetTouchEventFingerId(event),
        OH_Input_GetTouchEventDisplayX(event), OH_Input_GetTouchEventDisplayY(event),
        static_cast<int32_t>(OH_Input_GetTouchEventToolType(event)),
        OH_Input_GetTouchEventWindowId(event), OH_Input_GetTouchEventDisplayId(event),
        OH_Input_GetTouchEventActionTime(event), OH_Input_GetTouchEventDownTime(event),
        OH_Input_GetTouchEventPressure(event)};
    if (point && clockRecorder && receipt.after > 0) clockRecorder->Observe(point->seq, point->time, receipt);
    if (!point || napi_call_threadsafe_function(session->sink, point, napi_tsfn_nonblocking) != napi_ok) {
        delete point;
        ++dropped;
    }
}

int StopCapture()
{
    CaptureSession *completed;
    {
        std::lock_guard<std::mutex> guard(captureMutex);
        if (!running.exchange(false)) return 0;
        completed = session;
    }
    // Do not lock around removal: it can wait for an input callback to finish.
    int result = OH_Input_RemoveTouchEventMonitor(OnTouch);
    completed->stopCode = result;
    napi_release_threadsafe_function(completed->sink, napi_tsfn_release);
    return result;
}

napi_value Start(napi_env env, napi_callback_info info)
{
    napi_value callback, resource, result;
    size_t argc = 1;
    napi_get_cb_info(env, info, &argc, &callback, nullptr, nullptr);
    napi_valuetype type;
    if (argc != 1 || napi_typeof(env, callback, &type) != napi_ok || type != napi_function) {
        napi_throw_type_error(env, nullptr, "start requires a callback");
        return nullptr;
    }
    if (running.load()) {
        napi_create_int32(env, 0, &result);
        return result;
    }
    if (session) {
        napi_throw_error(env, nullptr, "The previous capture is still closing");
        return nullptr;
    }
    count = 0;
    dropped = 0;
    napi_create_string_utf8(env, "InkInput", NAPI_AUTO_LENGTH, &resource);
    auto created = new (std::nothrow) CaptureSession;
    if (!created) {
        napi_throw_error(env, nullptr, "Unable to allocate capture session");
        return nullptr;
    }
    if (napi_create_threadsafe_function(env, callback, nullptr, resource, 2048, 1,
                                       created, FinalizeSession, nullptr, Deliver, &created->sink) != napi_ok) {
        delete created;
        napi_throw_error(env, nullptr, "Unable to create input callback queue");
        return nullptr;
    }
    {
        std::lock_guard<std::mutex> guard(captureMutex);
        session = created;
        running = true;
    }
    int code = OH_Input_AddTouchEventMonitor(OnTouch);
    if (code != 0) {
        {
            std::lock_guard<std::mutex> guard(captureMutex);
            running = false;
        }
        created->stopCode = code;
        napi_release_threadsafe_function(created->sink, napi_tsfn_abort);
    }
    napi_create_int32(env, code, &result);
    return result;
}

napi_value Stop(napi_env env, napi_callback_info)
{
    napi_value promise;
    napi_deferred deferred;
    napi_create_promise(env, &deferred, &promise);
    bool pending;
    {
        std::lock_guard<std::mutex> guard(captureMutex);
        pending = session != nullptr;
        if (pending) session->stops.push_back(deferred);
    }
    if (pending) StopCapture();
    else {
        napi_value result;
        napi_create_int32(env, 0, &result);
        napi_resolve_deferred(env, deferred, result);
    }
    return promise;
}

napi_value Status(napi_env env, napi_callback_info)
{
    napi_value object, state;
    napi_create_object(env, &object);
    napi_get_boolean(env, running.load(), &state);
    napi_set_named_property(env, object, "running", state);
    Number(env, object, "point_count", count.load());
    Number(env, object, "dropped_count", dropped.load());
    return object;
}

napi_value ClockSample(napi_env env, napi_callback_info)
{
    timespec before{}, wall{}, boot{}, after{};
    if (clock_gettime(CLOCK_MONOTONIC, &before) || clock_gettime(CLOCK_REALTIME, &wall) ||
        clock_gettime(CLOCK_BOOTTIME, &boot) || clock_gettime(CLOCK_MONOTONIC, &after)) {
        napi_throw_error(env, nullptr, "Unable to sample device clocks");
        return nullptr;
    }
    auto micros = [](const timespec &t) { return t.tv_sec * 1000000.0 + t.tv_nsec / 1000.0; };
    napi_value object;
    napi_create_object(env, &object);
    Number(env, object, "monotonic_before_us", micros(before));
    Number(env, object, "monotonic_after_us", micros(after));
    Number(env, object, "boottime_us", micros(boot));
    Number(env, object, "unix_ms", micros(wall) / 1000.0);
    return object;
}

napi_value StartClock(napi_env env, napi_callback_info info)
{
    napi_value args[2], result;
    size_t argc = 2, directoryLength = 0, sessionLength = 0;
    char directory[256]{}, id[81]{};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
    bool ok = argc == 2 && !clockRecorder &&
        napi_get_value_string_utf8(env, args[0], directory, sizeof(directory), &directoryLength) == napi_ok &&
        napi_get_value_string_utf8(env, args[1], id, sizeof(id), &sessionLength) == napi_ok;
    std::string sessionId(id);
    ok = ok && std::string(directory) == "/data/storage/el2/base/haps/entry/files" &&
        sessionLength > 0 && sessionLength < sizeof(id) - 1 &&
        sessionId.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-") == std::string::npos;
    if (ok) {
        try {
            const std::string path = std::string(directory) + "/ink-" + sessionId;
            auto created = std::make_unique<CaptureClockRecorder>(path, sessionId, ReadCaptureClock, WriteClockFile);
            std::lock_guard<std::mutex> guard(captureMutex);
            clockRecorder = std::move(created);
        } catch (...) { ok = false; }
    }
    napi_get_boolean(env, ok, &result);
    return result;
}

napi_value StopClock(napi_env env, napi_callback_info)
{
    EndClock();
    napi_value result;
    napi_get_undefined(env, &result);
    return result;
}

void Cleanup(void *) { StopCapture(); EndClock(); }
napi_value Init(napi_env env, napi_value exports)
{
    napi_property_descriptor properties[] = {
        {"start", nullptr, Start, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"stop", nullptr, Stop, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"status", nullptr, Status, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"clockSample", nullptr, ClockSample, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"startClock", nullptr, StartClock, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"stopClock", nullptr, StopClock, nullptr, nullptr, nullptr, napi_default, nullptr}};
    napi_define_properties(env, exports, 6, properties);
    RegisterScreenSession(env, exports);
    napi_add_env_cleanup_hook(env, Cleanup, nullptr);
    return exports;
}
}

static napi_module module = {1, 0, nullptr, Init, "ink_native", nullptr, {0}};
extern "C" __attribute__((constructor)) void RegisterInkModule() { napi_module_register(&module); }
