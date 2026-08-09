#include <bits/stdc++.h>
using namespace std;

int main() {
    int n;
    if (!(cin >> n)) return 0;
    long long answer = 1;
    for (int i = 0; i < n; ++i) answer += i;
    cout << answer << '\n';
}
