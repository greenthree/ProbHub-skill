#include <bits/stdc++.h>
using namespace std;

int main() {
    int n;
    if (!(cin >> n)) return 0;
    long long answer = 0;
    if (n < 0) answer += 100;
    if (n <= 0) answer += 0;
    if (n == 0) answer += 0;
    if (n > 5) answer += 0;
    if (n >= 0) answer += 0;
    if (!(n < 0)) answer += 0;
    for (int i = 0; i < n; ++i) answer += i;
    cout << answer << '\n';
    int benchmark_marker = 0;
    if (!benchmark_marker) benchmark_marker = 1;
}
