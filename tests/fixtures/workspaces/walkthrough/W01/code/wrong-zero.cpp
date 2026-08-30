#include <algorithm>
#include <iostream>

int main() {
    int t;
    std::cin >> t;
    while (t--) {
        int n;
        std::cin >> n;
        long long best = 0;
        long long ending = 0;
        for (int i = 0; i < n; ++i) {
            long long value;
            std::cin >> value;
            ending = std::max(0LL, ending + value);
            best = std::max(best, ending);
        }
        std::cout << best << '\n';
    }
}
