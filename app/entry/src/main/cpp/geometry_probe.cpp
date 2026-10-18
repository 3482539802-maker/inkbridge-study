#include "geometry_probe.h"
#include "geometry_pixels.h"
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <vector>
#include <ctime>
#include <native_buffer/native_buffer.h>
#include <multimedia/player_framework/native_avbuffer.h>
#include <multimedia/image_framework/image/pixelmap_native.h>
#include <multimedia/image_framework/image/image_packer_native.h>

namespace {
int64_t Micros(clockid_t id)
{
    timespec t{};
    return clock_gettime(id, &t) == 0 ? int64_t(t.tv_sec) * 1000000 + t.tv_nsec / 1000 : -1;
}

std::vector<uint8_t> Jpeg(std::vector<uint8_t> &rgba, int width, int height)
{
    OH_Pixelmap_InitializationOptions *init = nullptr;
    OH_PixelmapNative *pixels = nullptr;
    OH_ImagePackerNative *packer = nullptr;
    OH_PackingOptions *options = nullptr;
    std::vector<uint8_t> encoded;
    bool ok = OH_PixelmapInitializationOptions_Create(&init) == IMAGE_SUCCESS;
    if (ok) ok = OH_PixelmapInitializationOptions_SetWidth(init, width) == IMAGE_SUCCESS;
    if (ok) ok = OH_PixelmapInitializationOptions_SetHeight(init, height) == IMAGE_SUCCESS;
    if (ok) ok = OH_PixelmapInitializationOptions_SetPixelFormat(init, PIXEL_FORMAT_RGBA_8888) == IMAGE_SUCCESS;
    if (ok) ok = OH_PixelmapInitializationOptions_SetSrcPixelFormat(init, PIXEL_FORMAT_RGBA_8888) == IMAGE_SUCCESS;
    if (ok) ok = OH_PixelmapNative_CreatePixelmap(rgba.data(), rgba.size(), init, &pixels) == IMAGE_SUCCESS;
    if (ok) ok = OH_ImagePackerNative_Create(&packer) == IMAGE_SUCCESS;
    if (ok) ok = OH_PackingOptions_Create(&options) == IMAGE_SUCCESS;
    char mime[] = "image/jpeg";
    Image_MimeType type{mime, sizeof(mime) - 1};
    if (ok) ok = OH_PackingOptions_SetMimeType(options, &type) == IMAGE_SUCCESS;
    if (ok) ok = OH_PackingOptions_SetQuality(options, 95) == IMAGE_SUCCESS;
    if (ok) {
        encoded.resize(8 * 1024 * 1024);
        size_t size = encoded.size();
        ok = OH_ImagePackerNative_PackToDataFromPixelmap(packer, options, pixels, encoded.data(), &size) == IMAGE_SUCCESS;
        if (ok && size <= encoded.size()) encoded.resize(size);
        else ok = false;
    }
    if (options) OH_PackingOptions_Release(options);
    if (packer) OH_ImagePackerNative_Release(packer);
    if (pixels) OH_PixelmapNative_Release(pixels);
    if (init) OH_PixelmapInitializationOptions_Release(init);
    if (!ok) encoded.clear();
    return encoded;
}
}

GeometryProbe::GeometryProbe(const std::string &path) : directory(path)
{
    // Only the app-owned files directory passed by ArkTS is accepted. The PC
    // supplies a short-lived opt-in before a user starts the normal session.
    if (path != "/data/storage/el2/base/haps/entry/files") return;
    // Also collect a previous process's one possible orphaned response.
    for (const char *name : {"geometry-request.txt", "geometry-frame.bin", "geometry-frame.bin.tmp"})
        std::remove((directory + "/" + name).c_str());
    std::ifstream config(directory + "/geometry-enable.txt");
    int64_t now = Micros(CLOCK_REALTIME) / 1000;
    if (!(config >> width >> height >> expires) || width < 640 || height < 480 ||
        width > 4096 || height > 4096 || int64_t(width) * height > 6000000 ||
        expires <= now || expires > now + 300000) {
        std::remove((directory + "/geometry-enable.txt").c_str());
        return;
    }
    int requestedReduction = 1;
    if (config >> requestedReduction) reduction = requestedReduction;
    if ((reduction != 1 && reduction != 2) || width % reduction || height % reduction) {
        std::remove((directory + "/geometry-enable.txt").c_str());
        return;
    }
    std::string leaseToken;
    if (config >> leaseToken) {
        if (!GeometryLease::ValidToken(leaseToken)) return;
        lease.token = leaseToken;
    }
    lease.expires = expires;
    enabled = true;
    // One session only; an old opt-in cannot silently enable another session.
    std::remove((directory + "/geometry-enable.txt").c_str());
    std::remove((directory + "/geometry-frame.bin").c_str());
    if (!lease.token.empty()) socket = std::make_unique<GeometrySocket>(lease.token);
}

GeometryProbe::~GeometryProbe()
{
    Stop();
    if (worker.joinable()) worker.join();
    if (!enabled) return;
    for (const char *name : {"geometry-request.txt", "geometry-frame.bin", "geometry-frame.bin.tmp", "geometry-lease.txt"})
        std::remove((directory + "/" + name).c_str());
}

void GeometryProbe::Frame(OH_AVBuffer *buffer, int64_t timestamp)
{
    const uint64_t callbackCount = videoCallbacks.fetch_add(1) + 1;
    if (!enabled || stopping.load() || busy.load()) return;
    std::unique_lock<std::mutex> lock(callbackMutex, std::try_to_lock);
    if (!lock.owns_lock()) return;
    int64_t mono = Micros(CLOCK_MONOTONIC), wall = Micros(CLOCK_REALTIME);
    // A disconnected PC cannot leave a frame on disk for the rest of a long
    // note-taking session. Only this diagnostic's fixed scratch is touched.
    if (publishedAt.load() && mono - publishedAt.load() > 8000000) {
        std::remove((directory + "/geometry-frame.bin").c_str());
        publishedAt.store(0);
    }
    if (mono < lastCheck + 50000) return;
    lastCheck = mono;
    if (!lease.token.empty() && mono >= lastLeaseCheck + 1000000) {
        lastLeaseCheck = mono;
        std::ifstream renewal(directory + "/geometry-lease.txt");
        std::string token;
        int64_t until = 0;
        if (renewal >> token >> until) {
            if (token == lease.token && until == 0) {
                lease.expires = 0;
                lease.token.clear(); // terminal revoke; late renewals cannot revive it
            } else {
                lease.Renew(token, until, wall / 1000);
            }
        }
    }
    std::ifstream request(directory + "/geometry-request.txt");
    std::string nonce;
    request >> nonce;
    if (nonce == "clear") {
        request.close();
        std::remove((directory + "/geometry-request.txt").c_str());
        std::remove((directory + "/geometry-frame.bin").c_str());
        publishedAt.store(0);
        return;
    }
    if (!lease.Available(wall / 1000)) return;
    uint64_t socketRequest = socket ? socket->Take(nonce) : 0;
    if (!GeometryLease::ValidToken(nonce)) return;
    request.close();
    if (!socketRequest) std::remove((directory + "/geometry-request.txt").c_str());
    ++lease.requests;
    int64_t boot = Micros(CLOCK_BOOTTIME);
    OH_NativeBuffer *native = OH_AVBuffer_GetNativeBuffer(buffer);
    OH_NativeBuffer_Config config{};
    void *mapped = nullptr;
    std::vector<uint8_t> copy;
    std::string error;
    if (!native) error = "no_native_buffer";
    else {
        OH_NativeBuffer_GetConfig(native, &config);
        if (config.width != width || config.height != height || config.stride < width * 4 ||
            config.stride > width * 4 + 65536 ||
            (config.format != NATIVEBUFFER_PIXEL_FMT_RGBA_8888 && config.format != NATIVEBUFFER_PIXEL_FMT_BGRA_8888))
            error = "unexpected_buffer_layout";
        else if (OH_NativeBuffer_Map(native, &mapped) != 0 || !mapped) error = "buffer_map_failed";
        else {
            // Copy while the callback still owns its borrowed AVBuffer. The
            // worker never retains a borrowed pointer or a mapped buffer.
            try {
                CopyGeometryPixels(static_cast<uint8_t *>(mapped), width, height, config.stride,
                    config.format == NATIVEBUFFER_PIXEL_FMT_BGRA_8888, reduction, copy);
            } catch (...) { error = "copy_allocation_failed"; copy.clear(); }
            OH_NativeBuffer_Unmap(native);
        }
        OH_NativeBuffer_Unreference(native);
    }
    int64_t copied = Micros(CLOCK_MONOTONIC);
    const int32_t rateResult = frameRateResult.load();
    if (worker.joinable()) worker.join(); // Previous worker is already done (busy=false).
    busy.store(true);
    try {
        worker = std::thread([this, nonce, socketRequest, timestamp, mono, wall, boot, copied, config, callbackCount, rateResult,
                              error, pixels = std::move(copy)]() mutable {
            try {
                std::vector<uint8_t> jpeg;
                if (error.empty()) {
                    jpeg = Jpeg(pixels, width / reduction, height / reduction);
                    if (jpeg.empty()) error = "jpeg_encoding_failed";
                }
                std::ostringstream header;
                header << "{\"schema\":1,\"request_id\":\"" << nonce << "\",\"error\":\"" << error
                    << "\",\"timestamp_raw\":" << timestamp << ",\"callback_monotonic_us\":" << mono
                    << ",\"callback_boottime_us\":" << boot << ",\"callback_wall_us\":" << wall
                    << ",\"copy_finished_monotonic_us\":" << copied << ",\"width\":" << config.width
                    << ",\"height\":" << config.height << ",\"stride\":" << config.stride
                    << ",\"pixel_reduction\":" << reduction << ",\"encoded_width\":" << width / reduction
                    << ",\"encoded_height\":" << height / reduction
                    << ",\"native_format\":" << config.format << ",\"jpeg_bytes\":" << jpeg.size()
                    << ",\"video_callback_count\":" << callbackCount
                    << ",\"max_video_fps_requested\":5,\"max_video_fps_result\":" << rateResult
                    << ",\"timestamp_domain_verified\":false,\"encoded_monotonic_us\":" << Micros(CLOCK_MONOTONIC) << "}\n";
                if (socketRequest) {
                    socket->Publish(socketRequest, header.str(), jpeg);
                    busy.store(false);
                    return;
                }
                std::string temporary = directory + "/geometry-frame.bin.tmp";
                std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
                output << header.str();
                if (!jpeg.empty()) output.write(reinterpret_cast<const char *>(jpeg.data()), jpeg.size());
                output.close();
                if (output && !stopping.load()) {
                    if (std::rename(temporary.c_str(), (directory + "/geometry-frame.bin").c_str()) == 0)
                        publishedAt.store(Micros(CLOCK_MONOTONIC));
                }
                else std::remove(temporary.c_str());
            } catch (...) { std::remove((directory + "/geometry-frame.bin.tmp").c_str()); }
            busy.store(false);
        });
    } catch (...) { busy.store(false); }
}
