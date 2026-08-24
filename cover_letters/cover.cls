% Intro Options
\ProvidesClass{cover}[2024/04/30 Cover letter class]
\NeedsTeXFormat{LaTeX2e}
\DeclareOption{print}{\def\@cv@print{}}
\DeclareOption*{%
  \PassOptionsToClass{\CurrentOption}{article}
}
\ProcessOptions\relax
\LoadClass{article}

% Package Imports
\usepackage[hmargin=2.6cm, vmargin=2.3cm]{geometry}
\usepackage[hidelinks]{hyperref}
\usepackage[usenames,dvipsnames]{xcolor}
\usepackage{titlesec}
\usepackage[absolute]{textpos}
\usepackage{fontspec,xltxtra,xunicode}
\usepackage[lining]{XCharter}

% Publications
\usepackage{cite}
\renewcommand\refname{\vskip -1.5cm}

% Color definitions
\definecolor{date}{HTML}{666666}
\definecolor{primary}{HTML}{2b2b2b}
\definecolor{headings}{HTML}{6A6A6A}
\definecolor{subheadings}{HTML}{333333}
\definecolor{accentcolor}{RGB}{26,66,138}

% XCharter is the body family, matching the CV. Sans faces are reserved for
% the sender block, metadata and subject hierarchy.
\setsansfont[Scale=MatchLowercase,Mapping=tex-text,Path=OpenFonts/fonts/raleway/,Extension=.otf]{Raleway-ExtraLight}
\newfontfamily\latoheavyfont[Path=OpenFonts/fonts/lato/,Extension=.ttf]{Lato-Hai}
\newfontfamily\latolightfont[Path=OpenFonts/fonts/lato/,Extension=.ttf]{Lato-Lig}
\newfontfamily\latoboldfont[Path=OpenFonts/fonts/lato/,Extension=.ttf]{Lato-Bol}
\newfontfamily\latoregularfont[Path=OpenFonts/fonts/lato/,Extension=.ttf]{Lato-Reg}
\newfontfamily\ralewaymediumfont[Path=OpenFonts/fonts/raleway/,Extension=.otf]{Raleway-Medium}
\newfontfamily\ralewayextralightfont[Path=OpenFonts/fonts/raleway/,Extension=.otf]{Raleway-ExtraLight}
\newcommand{\letterbodyfont}{\rmfamily\fontsize{10.7pt}{14pt}\selectfont}

% Date command
\setlength{\TPHorizModule}{1mm}
\setlength{\TPVertModule}{1mm}
\newcommand{\lastupdated}{\begin{textblock}{60}(155,5)
\color{date}\ralewayextralightfont\fontsize{8pt}{10pt}\selectfont
Last Updated on \today
\end{textblock}}

% Name command
\newcommand{\namesection}[3]{
\centering{
\fontsize{40pt}{60pt}
\latoheavyfont\selectfont #1
\latolightfont\selectfont #2
} \\[5pt]
\centering{
\color{headings}
\ralewaymediumfont\fontsize{11pt}{14pt}\selectfont #3}
\noindent\makebox[\linewidth]{\color{headings}\rule{\paperwidth}{0.0pt}}
\vspace{0pt}
}

% Section seperators
\titlespacing{\section}{0pt}{0pt}{0pt}
\titlespacing{\subsection}{0pt}{0pt}{0pt}
\newcommand{\sectionsep}{\vspace{8pt}}

% Headings command
\titleformat{\section}{\color{headings}
\scshape\latolightfont\fontsize{16pt}{24pt}\selectfont \raggedright\uppercase}{}{0em}{}

% Subeadings command
\titleformat{\subsection}{
\color{subheadings}\latoboldfont\fontsize{12pt}{12pt}\selectfont\uppercase}{}{0em}{}

\newcommand{\runsubsection}[1]{
\color{subheadings}\latoboldfont\fontsize{12pt}{12pt}\selectfont\uppercase {#1} \normalfont}

% Descriptors command
\newcommand{\descript}[1]{
\color{subheadings}\raggedright\scshape\ralewaymediumfont\fontsize{11pt}{13pt}\selectfont {#1 \\} \normalfont}

% Location command
\newcommand{\location}[1]{
\color{headings}\raggedright\ralewaymediumfont\fontsize{10pt}{12pt}\selectfont {#1\\} \normalfont}

% Bullet Lists with fewer gaps command
\newenvironment{tightemize}{
\vspace{-\topsep}\begin{itemize}\itemsep1pt \parskip0pt \parsep0pt}
{\end{itemize}\vspace{-\topsep}}

% Cover Letter
\newcommand{\companyname}[1]{\raggedright\latoboldfont\fontsize{12pt}{14pt}\selectfont {#1 \\} \normalfont}

\newcommand{\companyaddress}[1]{\raggedright\ralewaymediumfont\fontsize{11pt}{13pt}\selectfont {#1 \\}\mbox{}\\\mbox{}\\ \normalfont}

\newcommand{\currentdate}[1]{{\raggedleft\ralewaymediumfont\fontsize{11pt}{13pt}\selectfont #1\par}}

% Letter content command
\newcommand{\lettercontent}[1]{\raggedright\letterbodyfont {#1 \\}\mbox{}\\ \normalfont}

\newcommand{\closing}[1]{\raggedright\letterbodyfont {#1 \\}\mbox{}\\\mbox{}\\ \normalfont}

\newcommand{\signature}[1]{\raggedright\letterbodyfont {#1 \\} \normalfont}

% Formal Swiss/German business-letter blocks
\newcommand{\senderblock}[2]{%
\begin{flushright}
{\latoboldfont\fontsize{16pt}{19pt}\selectfont #1\par}
\vspace{2pt}
{\color{headings}\latoregularfont\fontsize{9.5pt}{12pt}\selectfont #2\par}
\vspace{2.5mm}
{\color{accentcolor}\rule{7.2cm}{0.5pt}\par}
\end{flushright}
\vspace{4mm}
}

\newcommand{\recipientblock}[1]{%
{\raggedright\rmfamily\fontsize{10.5pt}{13pt}\selectfont #1\par}
\vspace{3mm}
}

\newcommand{\subjectline}[1]{%
\vspace{2mm}
{\color{accentcolor}\raggedright\noindent\latoboldfont\fontsize{11pt}{13pt}\selectfont #1\par}
\vspace{4mm}
}
