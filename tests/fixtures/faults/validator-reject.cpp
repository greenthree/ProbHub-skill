#include "testlib.h"

int main(int argc, char** argv) {
    registerValidation(argc, argv);
    int x = inf.readInt(-100, 100, "x");
    inf.readEoln();
    inf.readEof();
    ensuref(x != 3, "fixture rejects x=3");
}
