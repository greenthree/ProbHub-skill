#include <iostream>

int main() {
    for (int i = 0; i < 2 * 1024 * 1024; ++i)
        std::cout.put('x');
}
