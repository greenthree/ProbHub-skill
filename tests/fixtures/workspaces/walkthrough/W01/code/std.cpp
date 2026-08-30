#include <algorithm>
#include <iostream>
#include <limits>

int main() {
    int t;
    std::cin >> t;
    while (t--) {
        int n;
        std::cin >> n;
        long long best = std::numeric_limits<long long>::lowest();
        long long ending = 0;
        for (int i = 0; i < n; ++i) {
            long long value;
            std::cin >> value;
            ending = (i == 0) ? value : std::max(value, ending + value);
            best = std::max(best, ending);
        }
        std::cout << best << '\n';
    }
}
