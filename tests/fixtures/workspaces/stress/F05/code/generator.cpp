#include <cstdlib>
#include <iostream>

int main(int argc, char** argv) {
    if (argc < 2)
        return 1;
    unsigned long long seed = std::strtoull(argv[1], nullptr, 10);
    std::cout << seed % 101 << '\n';
}
