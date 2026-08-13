#include "testlib.h"

int main() {
    registerValidation();
    inf.readInt(0, 20000000, "n");
    inf.readEoln();
    inf.readEof();
    return 0;
}
