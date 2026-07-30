#include <iostream>

int main() {
    int x;
    std::cin >> x;
    long long answer = 0;
    for (int i = 0; i < x; ++i)
        answer += x;
    std::cout << answer << '\n';
}
