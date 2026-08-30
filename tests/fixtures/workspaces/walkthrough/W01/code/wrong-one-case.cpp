#include <algorithm>
#include <iostream>

int main() {
    int t;
    std::cin >> t;
    if (t == 0) return 0;
    int n;
    std::cin >> n;
    long long best = 0;
    long long ending = 0;
    for (int i = 0; i < n; ++i) {
        long long value;
        std::cin >> value;
        ending = (i == 0) ? value : std::max(value, ending + value);
        best = (i == 0) ? ending : std::max(best, ending);
    }
    std::cout << best << '\n';
}
