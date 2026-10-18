#pragma once
#include <cstdint>
#include <cstring>
#include <vector>
#include <algorithm>

// Exact software mapping: destination (x,y) averages source (2x..2x+1,
// 2y..2y+1), whose pixel-centre location is (2x+.5,2y+.5).
inline void CopyGeometryPixels(const uint8_t *source, int width, int height,
    int stride, bool bgra, int reduction, std::vector<uint8_t> &output)
{
    const int targetWidth = width / reduction, targetHeight = height / reduction;
    output.resize(size_t(targetWidth) * targetHeight * 4);
    for (int y = 0; y < targetHeight; ++y) {
        uint8_t *destination = output.data() + size_t(y) * targetWidth * 4;
        const uint8_t *row = source + size_t(y * reduction) * stride;
        if (reduction == 1) std::memcpy(destination, row, size_t(width) * 4);
        else {
            const uint8_t *next = row + stride;
            for (int x = 0; x < targetWidth; ++x)
                for (int c = 0; c < 4; ++c) {
                    const int i = x * 8 + c;
                    destination[x * 4 + c] = (unsigned(row[i]) + row[i+4] + next[i] + next[i+4] + 2) / 4;
                }
        }
        if (bgra)
            for (int x = 0; x < targetWidth; ++x) std::swap(destination[x*4], destination[x*4+2]);
    }
}
