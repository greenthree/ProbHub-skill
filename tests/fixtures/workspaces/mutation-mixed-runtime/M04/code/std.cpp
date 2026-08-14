#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int rows, columns, period, seed;
    if (!(cin >> rows >> columns >> period >> seed)) return 0;

    if (rows == 1 || columns == 1) {
        cout << 1LL * rows * columns << '\n';
        return 0;
    }

    auto inside = [rows, columns](int row, int column) {
        return row >= 0 && row < rows && column >= 0 && column < columns;
    };
    auto blocked = [period, seed](int row, int column) {
        if (row == 0 || column == 0) return false;
        long long signature = 37LL * row + 61LL * column + seed;
        return signature % period == 0;
    };

    const int cells = rows * columns;
    vector<unsigned char> seen(cells, 0);
    queue<int> pending;
    seen[0] = 1;
    pending.push(0);
    long long reachable = 0;
    const int dr[4] = {-1, 1, 0, 0};
    const int dc[4] = {0, 0, -1, 1};

    while (!pending.empty()) {
        int current = pending.front();
        pending.pop();
        ++reachable;
        int row = current / columns;
        int column = current % columns;
        for (int direction = 0; direction < 4; ++direction) {
            int next_row = row + dr[direction];
            int next_column = column + dc[direction];
            if (!inside(next_row, next_column) || blocked(next_row, next_column)) continue;
            int next = next_row * columns + next_column;
            if (seen[next] != 0) continue;
            seen[next] = 1;
            pending.push(next);
        }
    }

    cout << reachable << '\n';
}
