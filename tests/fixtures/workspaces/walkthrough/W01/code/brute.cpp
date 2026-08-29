#include <algorithm>
#include <iostream>
#include <limits>
#include <vector>

int main() {
    int t;
    std::cin >> t;
    while (t--) {
        int n;
        std::cin >> n;
        std::vector<long long> a(n);
        for (long long &value : a) std::cin >> value;
        long long best = std::numeric_limits<long long>::lowest();
        for (int left = 0; left < n; ++left) {
            long long sum = 0;
            for (int right = left; right < n; ++right) {
                sum += a[right];
                best = std::max(best, sum);
            }
        }
        std::cout << best << '\n';
    }
}
