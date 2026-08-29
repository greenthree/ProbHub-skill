#include "testlib.h"

int main(int argc, char **argv) {
    registerValidation(argc, argv);
    int t = inf.readInt(1, 5, "T");
    inf.readEoln();
    long long total_n = 0;
    for (int tc = 0; tc < t; ++tc) {
        int n = inf.readInt(1, 20, "n");
        inf.readEoln();
        total_n += n;
        for (int i = 0; i < n; ++i) {
            inf.readInt(-1000, 1000, "a_i");
            if (i + 1 < n) inf.readSpace();
        }
        inf.readEoln();
    }
    ensuref(total_n <= 64, "sum of n is too large");
    inf.readEof();
}
