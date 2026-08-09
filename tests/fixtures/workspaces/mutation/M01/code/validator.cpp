#include "testlib.h"

int main() {
    registerValidation();
    inf.readInt(0, 5, "n");
    inf.readEoln();
    inf.readEof();
    return 0;
}
