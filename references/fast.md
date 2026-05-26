#### inmaker

```cpp
#include<bits/stdc++.h>
#define int long long
using namespace std;

mt19937_64 rnd(chrono::steady_clock::now().time_since_epoch().count());
int rd(int l, int r) { return uniform_int_distribution<int>(l, r)(rnd); }

// === 核心数据生成逻辑 ===
// 必须传入 ostream& 引用，才能写文件
void inmkr(ostream& fout)
{
    // 在此处写生成逻辑，例如：
    // int n = rd(1, 100);
    // fout << n << '\n';
}

signed main()
{
    for(int i = 1; i <= 20; i++)
    {
        ofstream fout(to_string(i) + ".in");
        
        // 调用数据生成函数
        inmkr(fout);

        fout.close();
    }
    return 0;
}

```

#### outmaker

```cpp
#include<bits/stdc++.h>
#define int long long
using namespace std;

// === 核心求解逻辑 (原 std) ===
// 必须传入 istream& 和 ostream& 引用，替代平时的 cin 和 cout
void _std(istream& fin, ostream& fout)
{
    // 在此处写求解逻辑：
    // int x; fin >> x; fout << x * 2 << '\n';
}

signed main()
{
    ios::sync_with_stdio(0);
    cin.tie(0);
    
    for(int i = 1; i <= 20; i++)
    {
        string ss = to_string(i);
        ifstream fin(ss + ".in");
        ofstream fout(ss + ".ans"); // 已修改为 .ans

        if (!fin.is_open()) continue;

        // 调用标准程序逻辑
        _std(fin, fout);

        fin.close();
        fout.close();
    }
    return 0;
}

```

**快捷替代方案：**
如果 `std.cpp` 已写好且从 `stdin` 读入，直接 bash 一行搞定，无需单独编译 `outmaker`：

```bash
for f in *.in; do ./std < "$f" > "${f%.in}.ans"; done

```

使用 `> "${f%.in}.ans"` 而非 `>.out` 是因为 DOMjudge 要求 `.ans` 后缀。
