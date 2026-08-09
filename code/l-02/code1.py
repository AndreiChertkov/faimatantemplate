def rating(p, checked_links, hallucinations):
    return 2 * p + checked_links - 3 * hallucinations

def report(name, p, checked_links, hallucinations):
    value = rating(p, checked_links, hallucinations)
    if hallucinations == 0 and value >= 10:
        decision = "допустить"
    else:
        decision = "отправить на коррекцию"
    return f"{name}: рейтинг {value}, {decision}"

print(report("A-17", 1, 2, 2))    # «\cmo{1}»
print(report("B-42", 3, 4, 0))    # «\cmo{2}»
print(report("C-314", 5, 3, 1))   # «\cmo{3}»