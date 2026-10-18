cmake_minimum_required(VERSION 3.5.0)
project(inkbridge)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_library(ink_native SHARED ink_native.cpp screen_session.cpp geometry_probe.cpp geometry_socket.cpp capture_clock.cpp)
# The HAP remains debuggable; pixel copying/format conversion must not run at
# the debug toolchain's unoptimized defaults on every requested full-size frame.
target_compile_options(ink_native PRIVATE -O2)
set_target_properties(ink_native PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/ink-output")
target_link_libraries(ink_native PUBLIC libace_napi.z.so libohinput.so libnative_avscreen_capture.so libhilog_ndk.z.so
    libnative_media_core.so libnative_buffer.so libpixelmap.so libimage_packer.so)
