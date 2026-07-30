#include <iomanip>
#include <iostream>

int main() {
    double x;
    std::cin >> x;
    std::cout << std::fixed << std::setprecision(1) << x / 3.0 << '\n';
}
