#include "geometry_socket.h"
#include <chrono>
#include <iostream>
#include <thread>

int main()
{
    GeometrySocket server(std::string(32, 'a'), 0);
    for (int i = 0; i < 200 && !server.Port(); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    if (!server.Port()) return 2;
    std::cout << server.Port() << std::endl;
    std::atomic<bool> stopped{false};
    std::thread producer([&] {
        while (!stopped.load()) {
            std::string nonce;
            if (uint64_t request = server.Take(nonce)) {
                if (nonce == std::string(32, 'f')) continue; // no video frame
                if (nonce == std::string(32, 'e')) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(200));
                }
                std::vector<uint8_t> data(128 * 1024, 42);
                if (nonce == std::string(32, 'd')) data.resize(8 * 1024 * 1024 + 8192, 42);
                server.Publish(request, "{\"request_id\":\""+nonce+"\"}\n", data);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    });
    std::string command;
    std::getline(std::cin, command);
    server.Stop(); stopped.store(true); producer.join();
    return 0;
}
