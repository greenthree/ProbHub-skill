#include "testlib.h"
#include <iostream>

int main(int argc, char **argv) {
    registerGen(argc, argv, 1);
    if (argc < 3) return 1;
    const std::string mode = argv[1];
    const int seed = std::atoi(argv[2]);
    rnd.setSeed(seed);
    const int t = mode == "many" ? 4 : 3;
    std::cout << t << '\n';
    int remaining = 64;
    for (int tc = 0; tc < t; ++tc) {
        int n = mode == "many" ? 16 : rnd.next(3, 8);
        n = std::min(n, remaining - (t - tc - 1));
        remaining -= n;
        std::cout << n << '\n';
        for (int i = 0; i < n; ++i) {
            if (i) std::cout << ' ';
            std::cout << rnd.next(-20, 20);
        }
        std::cout << '\n';
    }
}
