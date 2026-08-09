#include <array>
#include <compare>
#include <vector>

#define LESS_THAN(a, b) ((a) < (b))

template <class T, int N = (sizeof(T) < 8 ? 8 : sizeof(T))>
concept Small = requires(T value) {
    { value < 3 };
};

template <class T>
struct Box {
    T value;
    bool operator<(const Box& other) const { return value < other.value; }
    bool operator!() const { return value == 0; }
    auto operator<=>(const Box& other) const { return value <=> other.value; }
};

static_assert(sizeof(int) <= 8);

// UTF-8 before executable candidates: 中文
int solve(int x) {
    const char* text = "x < 3 && value == 0";
    std::vector<int> values;
    std::array<int, (sizeof(int) < 8 ? 8 : sizeof(int))> fixed{};
    if constexpr (sizeof(int) < 8) {
        x = x < 10 ? x : 10;
    }
    static_assert(noexcept(x < 3));
    switch (x) {
        case (1 < 2): x = x < 12 ? x : 12; break;
        default: break;
    }
    if (int y = x; y < 3) x = y;
    if (x <
        4) return x <= 2;
    do { --x; } while (!(x > 0));
    while (x != 1) --x;
    return 3 == x || LESS_THAN(x, 5);
}
