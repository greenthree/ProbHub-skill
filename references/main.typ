#import "../lib.typ": contest-conf, render-problem
#import "problems.typ": problems
#show: contest-conf.with(
  title: "Contest",
  subtitle: "正式赛",
  author: "",
  date: "",
  problems: problems,
  enable-titlepage: true,
  enable-header-footer: true,
  enable-problem-list: true,
  language: "zh",
  titlepage-language: "zh",
  problem-language: "zh",
)
