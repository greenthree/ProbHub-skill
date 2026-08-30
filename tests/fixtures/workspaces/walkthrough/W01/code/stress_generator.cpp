#include "testlib.h"
#include <iostream>

int main(int argc, char **argv) {
    registerGen(argc, argv, 1);
    if (argc < 3) return 1;
    rnd.setSeed(std::atoll(argv[1]) ^ (std::atoll(argv[2]) * 0x9e3779b9LL));
    const int t = rnd.next(1, 3);
    std::cout << t << '\n';
    int remaining = 64;
    for (int tc = 0; tc < t; ++tc) {
        const int n = rnd.next(1, std::min(12, remaining - (t - tc - 1)));
        remaining -= n;
        std::cout << n << '\n';
        for (int i = 0; i < n; ++i) {
            if (i) std::cout << ' ';
            std::cout << rnd.next(-30, 30);
        }
        std::cout << '\n';
    }
}
