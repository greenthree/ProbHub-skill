#import "@preview/numbly:0.1.0": numbly
#import "@preview/cmarker:0.1.6": render as cmarker-render
#import "@preview/mitex:0.2.6": *

#let md = cmarker-render.with(math: mitex, scope: (image: (source, alt: none, format: auto) => align(center)[#image(source, alt: alt, format: format)]))
#let fonts = (
  sans: ("Microsoft YaHei", "simsun"),
  song: ("New Computer Modern Math", "Simsun"),
  zsong: ("STZhongSong", "STZhongSong"),
  kaishu: ("FZKai-Z03",),
  mono: ("New Computer Modern Mono","New Computer Modern Mono")
)
#set text(font: fonts.song)
#let maketitle(
  title: none,
  subtitle: none,
  author: none,
  date: none,
) = {
  set align(center)
  set par(spacing: 0em)

  if title != none {
    text(2.2em, weight: "bold", font: fonts.sans, title)
    v(5em)
  }

  if subtitle != none {
    text(1.8em, weight: "bold", font: fonts.song, subtitle)
    v(5em)
  }

  if date != none {
    text(1.7em, date)
    v(5em)
  }

  // if author != none {
  //   text(1.5em, font: fonts.kaishu, author)
  //   // v(1.2em)
  // } 
}

#let translations = (
  zh: (
    input: "输入",
    output: "输出",
    examples: "样例",
    note: "注释",
    problem-list: "试题列表",
    stdin: "standard input",
    stdout: "standard output",
    problem-set-info: (n, m) => [本试题册共 #n 题，#m 页。],
    missing-warning: "如果您的试题册缺少页面，请立即通知志愿者。",
  ),
  en: (
    input: "Input",
    output: "Output",
    examples: "Examples",
    note: "Note",
    problem-list: "Problem List",
    stdin: "standard input",
    stdout: "standard output",
    problem-set-info: (n, m) => [This problem set should contain #n problems on #m numbered pages.],
    missing-warning: "Please inform a runner immediately if something is missing from your problem set.",
  )
)

#let render-problem(problem, statement, language: "zh") = [
  #v(-10pt)
  = #text(font: fonts.sans, size: 16pt)[#problem.display-name]
  // ================= 新增：副标题/引言渲染逻辑 =================
  #if statement.at("quote", default: none) != none [
    #v(2.8em) // 与上方标题的间距
    #align(right)[
      #box(width: auto)[
        #align(left)[#text(font: fonts.song, size: 11pt, statement.quote.at("text", default: ""))]
        #v(0.3em)
        #line(length: 45%, stroke: 0.5pt + black)
        // #v(0.3em)
        #align(right)[#text(font: fonts.song, size: 11pt, statement.quote.at("source", default: ""))]
      ]
    ]
    #v(0.7em) // 与下方正文的间距
  ] else [
    #v(10pt) // 如果没有引言，保持原有的标题下间距
  ]
  // =========================================================
  #v(10pt)
  #set par(spacing: 1.1em)
  #let format = problem.at("format", default: "latex")
  #if format == "latex" {
    let res = mitex-convert(mode: "text", statement.description)
    eval(res, mode: "markup", scope: mitex-scope)
  } else if format == "markdown" {
    md(statement.description)
  } else {
    eval(statement.description, mode: "markup")
  }

  #if statement.at("input", default: none) != none and statement.input != "" [
    #v(0.6em)
    == #text(font: fonts.sans, size: 14pt)[#translations.at(language).input]
    #v(0.5em)
    #set par(spacing: 1.1em)
    #if format == "latex" {
      let res = mitex-convert(mode: "text", statement.input)
      eval(res, mode: "markup", scope: mitex-scope)
    } else if format == "markdown" {
      md(statement.input)
    } else {
      eval(statement.input, mode: "markup")
    }
  ]
  #v(0.6em)

  #if statement.at("output", default: none) != none and statement.output != "" [
    == #text(font: fonts.sans, size: 14pt)[#translations.at(language).output]
    #v(0.5em)
    #set par(spacing: 1.1em)
    #if format == "latex" {
      let res = mitex-convert(mode: "text", statement.output)
      eval(res, mode: "markup", scope: mitex-scope)
    } else if format == "markdown" {
      md(statement.output)
    } else {
      eval(statement.output, mode: "markup")
    }
  ]
  #v(0.6em)

  #if problem.samples.len() > 0 [
    == #text(font: fonts.sans, size: 14pt)[#translations.at(language).examples]
    #set text(font: fonts.mono,size: 11pt)
    #v(0.3em)
    #figure(
      table(
        columns: (8.69cm, 8.68cm),
        align: (x, y) => if y == 0 { center } else { left },
        stroke: 0.4pt,
        table.header([#translations.at(language).stdin], [#translations.at(language).stdout]),
        ..problem.samples.map(s => {
        (
          // 使用局部作用域设置文本大小，不影响表格其他部分
          {
            set text(font: fonts.mono, size: 14pt)
            raw(s.input)
          },
          {
            set text(font: fonts.mono, size: 14pt)
            raw(s.output)
          }
        )
      }).flatten(),
        ),
    )
  ]
  #v(0.5em)

  #if statement.at("notes", default: none) != none and statement.notes != "" [
    == #text(font: fonts.sans, size: 14pt)[#translations.at(language).note]
    #set par(spacing: 1.1em)
     #v(0.5em)
    #if format == "latex" {
      let res = mitex-convert(mode: "text", statement.notes)
      eval(res, mode: "markup", scope: mitex-scope)
    } else if format == "markdown" {
      md(statement.notes)
    } else {
      eval(statement.notes, mode: "markup")
    }
  ]
]

#let render-problems(problems: none) = {}

#let contest-conf(
  title: "这是一场 XCPC 程序设计竞赛",
  subtitle: "热身赛",
  author: "初梦",
  date: datetime.today().display("[year] 年 [month] 月[day] 日"),
  problems: none,
  language: "zh",
  titlepage-language: auto,
  problem-language: auto,
  enable-titlepage: true,
  enable-header-footer: true,
  enable-problem-list: true,
  doc,
  
) = {
  let titlepage-lang = if titlepage-language == auto { language } else { titlepage-language }
  let problem-lang = if problem-language == auto { language } else { problem-language }
  set text(lang: "zh", font: fonts.song)
  set document(title: title, author: author)

  show strong: it => text(font: "SimSun", stroke: 0.3pt + black, it)
  show raw: set text(font: fonts.mono, size: 11pt)

  // 封面页
  if enable-titlepage {
    set page(
      margin: (top: 5cm, bottom: 3cm, left: 2.5cm, right: 2.5cm),
    )
    set par(spacing: 0.8em)
    maketitle(title: title, subtitle: subtitle, date: date, author: author)
    v(-2.2em) //正式赛
    // v(1.5em)
    align(center, image("usts.png", width: 9cm))
    // TOC
    if enable-problem-list {
      figure(
        placement: bottom,
        [
          #text(size: 15pt, font: fonts.zsong)[#translations.at(titlepage-lang).problem-list]
          #v(0.5em)
          #set table(stroke: (x, y) => (
            if y == 0 {
              if problems.len() == 1 {
                (top: 0.4pt, bottom: 0.4pt, left: 0.4pt, right: 0.4pt)
              } else {
                (top: 0.4pt, left: 0.4pt, right: 0.4pt)
              }
            } else if y == problems.len() - 1 {
              (bottom: 0.4pt, left: 0.4pt, right: 0.4pt)
            } else {
              (left: 0.4pt, right: 0.4pt)
            }
          ))
          #set text(size:15pt, font: fonts.song)
          #table(
            columns: (1cm, 10cm),
            align: center,
            inset: (x, y) => (
              top: if y == 0 { 7pt } else { 6pt },                       // 第一行上面间距调大到 5pt
              bottom: if y == problems.len() - 1 { 7pt } else { 6pt },   // 最后一行下面间距调大到 5pt
              left: 5pt,
              right: 5pt
            ),
            // stroke: 0.4pt,
            ..problems.enumerate().map(((i, e)) => (
              str.from-unicode(int(i) + 65), e.problem.display_name
            )).flatten()
          )

          #v(0.8cm)
          #set text(size:12pt, font: fonts.kaishu)
          #context (translations.at(titlepage-lang).problem-set-info)(problems.len(), counter(page).final().at(0))
          
          #translations.at(titlepage-lang).missing-warning
        ],
      )
    }
    // ================= 新增：强制插入一页空白页 =================
    pagebreak()
    box() // 放置一个不可见的空盒子，强制 Typst 渲染出这页空白
    // ============================================================
  }

  // 题面
  {
    set par(justify: true, spacing: 0.65em)
    show heading: set block(above: 0.6em)
    show heading: set text(font: fonts.sans)

    set page(
      margin: (top: 3cm, bottom: 2.5cm, x: 1.8cm),
      header: if enable-header-footer {
        [
          #set text(size: 10pt)
          #grid(
            columns: (1fr, 1fr),
            align: (left, right),
            [#title], [#date],
          )
          #v(-0.1cm)
          #line(length: 100%, stroke: 0.5pt)
        ]
      },
      footer: if enable-header-footer {
        context [
          #set align(center)
          #line(length: 100%, stroke: 0.5pt)
          #set text(font: fonts.kaishu)
          #counter(page).display(
            numbly("{1}", { "第 {1} 页，共{2}页" }),
            both: true,
          )
        ]
      },
    )

    counter(page).update(1)

    if problems != none {
      for (i, e) in problems.enumerate() {
        // ================= 修复：用 [ ] 切换到标记模式，并用 # 调用函数 =================
        [#box(width: 0pt, height: 0pt, metadata(e.problem.display_name)) <prob-boundary>]
        // =================================================================================
        e.problem.display-name = "题目 " + str.from-unicode(int(i) + 65) + ". " + e.problem.display_name
        render-problem(e.problem, e.statement, language: problem-lang)

        if i < problems.len() - 1 {
          pagebreak()
        }
      }
    }
  }

  doc
}