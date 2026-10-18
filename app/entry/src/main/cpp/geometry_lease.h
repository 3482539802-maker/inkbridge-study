#pragma once
#include <cstdint>
#include <string>

// A renewable geometry lease belongs only to the explicitly armed screen
// session. It never renews the OS screen authorization/background task.
struct GeometryLease {
    std::string token;
    int64_t expires = 0;
    unsigned requests = 0;
    static bool ValidToken(const std::string &s) {
        return s.size() == 32 && s.find_first_not_of("0123456789abcdef") == std::string::npos;
    }
    bool Renew(const std::string &incoming, int64_t until, int64_t now) {
        if (!ValidToken(token) || incoming != token || until <= now ||
            until > now + 300000 || until <= expires) return false;
        expires = until;
        requests = 0;
        return true;
    }
    bool Available(int64_t now) const { return now < expires && requests < 100; }
};
