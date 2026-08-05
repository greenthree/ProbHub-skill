#include "testlib.h"
#include <cstdlib>

int main(int argc, char** argv) {
    registerTestlibCmd(argc, argv);
    long long expected = ans.readLong();
    long long actual = ouf.readLong();
    if (!ouf.seekEof())
        quitf(_wa, "extra output");
    if (std::llabs(actual) == std::llabs(expected))
        quitf(_ok, "accepted absolute value");
    quitf(_wa, "wrong absolute value");
}
