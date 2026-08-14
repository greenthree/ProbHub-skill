#include "testlib.h"

int main() {
    registerValidation();
    int rows = inf.readInt(1, 1500, "h");
    inf.readSpace();
    int columns = inf.readInt(1, 1500, "w");
    inf.readSpace();
    int period = inf.readInt(2, 97, "p");
    inf.readSpace();
    int seed = inf.readInt(0, 96, "s");
    ensuref(seed < period, "s must be smaller than p");
    (void)rows;
    (void)columns;
    inf.readEoln();
    inf.readEof();
    return 0;
}
