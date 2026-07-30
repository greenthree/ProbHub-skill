#include "testlib.h"
#include <algorithm>
#include <cmath>

int main(int argc, char** argv) {
    registerTestlibCmd(argc, argv);
    double expected = ans.readDouble();
    double actual = ouf.readDouble();
    if (!std::isfinite(actual))
        quitf(_wa, "non-finite output");
    if (!ouf.seekEof())
        quitf(_wa, "extra output");
    double tolerance = std::max(1e-6, 1e-6 * std::abs(expected));
    if (std::abs(actual - expected) <= tolerance)
        quitf(_ok, "accepted");
    quitf(_wa, "outside tolerance");
}
