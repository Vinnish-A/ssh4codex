library(ggplot2)
library(qs)
data <- data.frame(x=1:10, y=(1:10)^2)
qsave(data, 'demo.qs')
ggsave('demo.png', ggplot(data, aes(x,y)) + geom_point() + theme_classic(),
       width=4.5, height=4.5, dpi=150)
