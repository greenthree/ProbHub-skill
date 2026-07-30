#include "testlib.h"

int main(int argc, char** argv) {
    registerTestlibCmd(argc, argv);
    quitf(_fail, "fixture checker failure");
}
