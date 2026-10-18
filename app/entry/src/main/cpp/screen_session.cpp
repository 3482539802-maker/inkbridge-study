#include <cstdint>
#include <new>
#include <memory>
#include "geometry_probe.h"
#include <multimedia/player_framework/native_avscreen_capture.h>
#include <napi/native_api.h>
#include <hilog/log.h>

namespace {
struct ScreenSession {
    OH_AVScreenCapture *capture = nullptr;
    napi_threadsafe_function callback = nullptr;
    std::unique_ptr<GeometryProbe> geometry;
    bool frameRateConfigured = false;
};
// All begin/end operations run on the ArkTS thread. Native callbacks only use
// their own session, which stays alive until Release completes and the queue drains.
ScreenSession *screen = nullptr;

int32_t CheckStage(const char *stage, int32_t code)
{
    OH_LOG_Print(LOG_APP, code ? LOG_ERROR : LOG_INFO, 0x0001, "InkBridge",
        "screen_setup stage=%{public}s code=%{public}d", stage, code);
    return code;
}

void DeliverState(napi_env env, napi_value callback, void *context, void *data)
{
    auto code = static_cast<int32_t *>(data);
    if (env && callback && code) {
        auto session = static_cast<ScreenSession *>(context);
        // The service requires CAP_ACTIVE: Init/Start request alone is not
        // sufficient. Apply once on the ArkTS thread after STARTED delivery,
        // never inside the native state callback or for an ended session.
        if (session && session == screen && *code == OH_SCREEN_CAPTURE_STATE_STARTED &&
            !session->frameRateConfigured && session->geometry && session->geometry->Enabled()) {
            session->frameRateConfigured = true;
            const int32_t rateResult = CheckStage("geometry_max_fps_5_after_started",
                OH_AVScreenCapture_SetMaxVideoFrameRate(session->capture, 5));
            session->geometry->FrameRateResult(rateResult);
        }
        napi_value value, receiver, ignored;
        napi_create_int32(env, *code, &value);
        napi_get_undefined(env, &receiver);
        napi_call_function(env, receiver, callback, 1, &value, &ignored);
    }
    delete code;
}

void SendState(ScreenSession *session, int32_t value)
{
    auto code = new (std::nothrow) int32_t(value);
    if (code && napi_call_threadsafe_function(session->callback, code, napi_tsfn_nonblocking) != napi_ok) delete code;
}

void OnState(OH_AVScreenCapture *, OH_AVScreenCaptureStateCode code, void *data)
{
    SendState(static_cast<ScreenSession *>(data), static_cast<int32_t>(code));
}

void OnError(OH_AVScreenCapture *, int32_t code, void *data)
{
    SendState(static_cast<ScreenSession *>(data), code > 0 ? -code : code);
}

void OnFrame(OH_AVScreenCapture *, OH_AVBuffer *buffer, OH_AVScreenCaptureBufferType type, int64_t timestamp, void *data)
{
    auto session = static_cast<ScreenSession *>(data);
    if (type == OH_SCREEN_CAPTURE_BUFFERTYPE_VIDEO && session->geometry)
        session->geometry->Frame(buffer, timestamp);
}

void EndSession()
{
    auto completed = screen;
    screen = nullptr;
    if (!completed) return;
    if (completed->geometry) completed->geometry->Stop();
    OH_AVScreenCapture_StopScreenCapture(completed->capture);
    OH_AVScreenCapture_Release(completed->capture);
    completed->geometry.reset(); // Join the sole worker, delete transient frame.
    napi_release_threadsafe_function(completed->callback, napi_tsfn_release);
}

napi_value Begin(napi_env env, napi_callback_info info)
{
    napi_value args[2], resource, result;
    size_t argc = 2;
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
    napi_value callback = argc ? args[0] : nullptr;
    napi_valuetype type;
    if (argc < 1 || napi_typeof(env, callback, &type) != napi_ok || type != napi_function) {
        napi_throw_type_error(env, nullptr, "beginScreen requires a state callback");
        return nullptr;
    }
    if (screen) {
        napi_throw_error(env, nullptr, "A screen session is already active");
        return nullptr;
    }
    auto created = new (std::nothrow) ScreenSession;
    if (!created) {
        napi_throw_error(env, nullptr, "Unable to allocate screen session");
        return nullptr;
    }
    if (argc == 2) {
        char directory[256]{};
        size_t length = 0;
        if (napi_get_value_string_utf8(env, args[1], directory, sizeof(directory), &length) == napi_ok && length < sizeof(directory) - 1)
            created->geometry = std::make_unique<GeometryProbe>(directory);
    }
    created->capture = OH_AVScreenCapture_Create();
    if (!created->capture) {
        delete created;
        napi_throw_error(env, nullptr, "Unable to create screen capture");
        return nullptr;
    }
    napi_create_string_utf8(env, "InkScreenSession", NAPI_AUTO_LENGTH, &resource);
    if (napi_create_threadsafe_function(env, callback, nullptr, resource, 32, 1,
        created, [](napi_env, void *data, void *) { delete static_cast<ScreenSession *>(data); },
        created, DeliverState, &created->callback) != napi_ok) {
        OH_AVScreenCapture_Release(created->capture);
        delete created;
        napi_throw_error(env, nullptr, "Unable to create screen state queue");
        return nullptr;
    }
    screen = created;
    OH_AVScreenCaptureConfig config{};
    config.captureMode = OH_CAPTURE_HOME_SCREEN;
    config.dataType = OH_ORIGINAL_STREAM;
    // Zero sample rates disable both microphone and internal audio.
    config.audioInfo.micCapInfo.audioSource = OH_MIC;
    config.audioInfo.innerCapInfo.audioSource = OH_ALL_PLAYBACK;
    config.videoInfo.videoCapInfo.videoFrameWidth = 320;
    config.videoInfo.videoCapInfo.videoFrameHeight = 208;
    if (created->geometry && created->geometry->Enabled()) {
        config.videoInfo.videoCapInfo.videoFrameWidth = created->geometry->Width();
        config.videoInfo.videoCapInfo.videoFrameHeight = created->geometry->Height();
    }
    config.videoInfo.videoCapInfo.videoSource = OH_VIDEO_SOURCE_SURFACE_RGBA;
    int32_t code = CheckStage("state_callback", OH_AVScreenCapture_SetStateCallback(created->capture, OnState, created));
    if (!code) code = CheckStage("error_callback", OH_AVScreenCapture_SetErrorCallback(created->capture, OnError, created));
    if (!code) code = CheckStage("data_callback", OH_AVScreenCapture_SetDataCallback(created->capture, OnFrame, created));
    if (!code) code = CheckStage("init", OH_AVScreenCapture_Init(created->capture, config));
    if (!code) code = CheckStage("disable_microphone", OH_AVScreenCapture_SetMicrophoneEnabled(created->capture, false));
    if (!code) code = CheckStage("start", OH_AVScreenCapture_StartScreenCapture(created->capture));
    if (code) EndSession();
    napi_create_int32(env, code, &result);
    return result;
}

napi_value End(napi_env env, napi_callback_info)
{
    EndSession();
    napi_value result;
    napi_get_undefined(env, &result);
    return result;
}
}

void RegisterScreenSession(napi_env env, napi_value exports)
{
    napi_property_descriptor properties[] = {
        {"beginScreen", nullptr, Begin, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"endScreen", nullptr, End, nullptr, nullptr, nullptr, napi_default, nullptr}};
    napi_define_properties(env, exports, 2, properties);
    napi_add_env_cleanup_hook(env, [](void *) { EndSession(); }, nullptr);
}
