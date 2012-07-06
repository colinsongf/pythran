#ifndef PYTHONIC_ITERATOR_H
#define PYTHONIC_ITERATOR_H
namespace pythonic {
    namespace __iterator__ {
        template <class T>
            decltype(*std::declval<T>()) next(T& y) { decltype(*std::declval<T>()) out = *y; ++y; return out ; }
        VPROXY(pythonic::__iterator__, next);
    }
}
#endif
