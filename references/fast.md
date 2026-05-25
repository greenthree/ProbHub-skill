# inmaker
请在 main 函数里自由发挥，使用 ofstream 输出数据。`rd(l, r)` 生成 `[l, r]` 内的整数。

```cpp
#include<bits/stdc++.h>
#define int long long
using namespace std;

mt19937_64 rnd(chrono::steady_clock::now().time_since_epoch().count());
int rd(int l, int r) { return uniform_int_distribution<int>(l, r)(rnd); }

signed main()
{
    for(int i = 1; i <= 20; i++)
    {
        ofstream fout(to_string(i) + ".in");

        // 在此处写生成逻辑，直接 fout << ... << '\n';

        fout.close();
    }
}
```

**注意：**
- 使用 `ofstream` 而非 `freopen`，避免 Windows 上缓冲区不刷新导致文件为空。
- 使用 `chrono::steady_clock` 而非 `time(0)` 作为种子，避免同秒内生成相同数据。
- `rd(l, r)` 是闭区间，`mt19937_64` 可生成 64 位随机数。

# outmaker
请在 main 函数里自由发挥，使用 ifstream 读入，ofstream 输出。

```cpp
#include<bits/stdc++.h>
#define int long long
using namespace std;

signed main()
{
    ios::sync_with_stdio(0);
    cin.tie(0);
    for(int i = 1; i <= 20; i++)
    {
        string ss = to_string(i);
        ifstream fin(ss + ".in");
        ofstream fout(ss + ".out");

        // 在此处写求解逻辑：fin >> x; fout << ans << '\n';

        fin.close();
        fout.close();
    }
}
```

**快捷替代方案：**
如果 std.cpp 已写好且从 stdin 读入，直接 bash 一行搞定，无需单独编译 outmaker：
```bash
for f in *.in; do ./std < "$f" > "${f%.in}.ans"; done
```
使用 `> "${f%.in}.ans"` 而非 `>.out` 是因为 DOMjudge 要求 `.ans` 后缀。
