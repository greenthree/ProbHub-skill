#import "../lib.typ": contest-conf, render-problem
#import "problems.typ": problems
#show: contest-conf.with(
  title: "第十二届苏州科技大学程序设计竞赛",
  subtitle: "正式赛",
  author: "USTS-ACM集训队",
  date: "2026年4月26日",
  problems: problems,
  enable-titlepage: true,
  enable-header-footer: true,
  enable-problem-list: true,
  language: "zh",
  titlepage-language: "zh",
  problem-language: "zh",
)
