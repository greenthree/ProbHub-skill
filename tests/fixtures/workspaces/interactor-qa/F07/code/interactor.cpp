#include "testlib.h"
#include <iostream>

int main(int argc, char** argv) {
    registerInteraction(argc, argv);
    long long secret = inf.readLong();
    std::cout << secret << std::endl;
    long long actual = ouf.readLong();
    if (actual == 2 * secret)
        quitf(_ok, "accepted");
    quitf(_wa, "wrong response");
}
