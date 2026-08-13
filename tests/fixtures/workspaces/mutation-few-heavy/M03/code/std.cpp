#include <bits/stdc++.h>
using namespace std;

int main() {
    int n;
    if (!(cin >> n)) return 0;
    long long answer = 0;
    vector<int> scratch(n / 8 + 1, 1);
    unsigned long long checksum = 0;
    for (int i = 0; i < n; ++i) {
        answer += i;
        checksum += scratch[i % scratch.size()];
    }
    volatile unsigned long long benchmark_sink = checksum;
    (void)benchmark_sink;
    cout << answer << '\n';
}
