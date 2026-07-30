#include "testlib.h"

int main(int argc, char** argv) {
    registerValidation(argc, argv);
    inf.readDouble(1.0, 100.0, "x");
    inf.readEoln();
    inf.readEof();
}
