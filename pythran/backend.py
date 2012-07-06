'''This module turns a pythran AST into C++ code
    *  cxx_backend generates a string holding the c++ code
'''
import ast
from cxxgen import *

from analysis import local_declarations, global_declarations, constant_value, yields

from tables import operator_to_lambda, modules, type_to_suffix
from typing import type_all
from syntax import PythranSyntaxError

templatize = lambda node, types: Template([ "typename " + t for t in types ], node ) if types else node 

class CxxBackend(ast.NodeVisitor):

    generator_state_holder = "__generator_state" # used to recover previous generator state

    def __init__(self, name):
        modules['__user__']=dict()
        self.name=name
        self.types=None
        self.declarations=list()
        self.definitions=list()
        self.break_handler=list()

    # mod
    def visit_Module(self, node):
        # build all types
        self.global_declarations = global_declarations(node)
        self.local_functions=set()
        self.local_declarations=list()
        self.types=type_all(node)
        headers= [ Include(h) for h in [ "pythran/pythran.h", "boost/python/module.hpp" ] ]
        body = [ self.visit(n) for n in node.body ]

        assert not self.local_declarations
        return headers +  [ Namespace("__{0}".format(self.name), body + self.declarations + self.definitions) ]

    # stmt
    def visit_FunctionDef(self, node):
        class CachedTypeVisitor:
            class CachedType:
                def __init__(self,s):self.s=s
                def generate(self,ctx): return self.s
            def __init__(self):
                self.cache=dict()
                self.mapping=dict()
            def __call__(self, node):
                if node not in self.mapping:
                    t=node.generate(self)
                    self.mapping[node]=len(self.mapping)
                    self.cache[node]=t
                return CachedTypeVisitor.CachedType("__type{0}".format(self.mapping[node]))
            def typedefs(self):
                l=sorted(self.mapping.items(),key=lambda x:x[1])
                L=list()
                for k,v in l:
                    #print "__type{0}".format(v), "->", k, ":", self.cache[k]
                    L.append( Typedef(Value(self.cache[k],"__type{0}".format(v))) )
                return L

        # prepare context and visit function body
        fargs = node.args.args

        local_functions={k for k in self.local_functions}

        formal_args = [ arg.id for arg in fargs ]
        formal_types= [ "argument_type"+str(i) for i in xrange(len(formal_args)) ]

        ldecls={ sym.id:sym for sym in local_declarations(node) }

        self.local_declarations.append(set(ldecls.iterkeys()))
        self.local_declarations[-1].update(formal_args)
        self.extra_declarations={}

        ldecls= set(ldecls.itervalues())

        self.yields = { k:(1+v,"yield_point{0}".format(1+v)) for (v,k) in enumerate(yields(node)) } # 0 is used as initial_state, thus the +1

        operator_body = [ self.visit(n) for n in node.body ] 

        self.local_declarations.pop()

        # add preprocessor line information
        if hasattr(node,"lineno"):
            operator_body.insert(0,Line('#line {0} "{1}.py"'.format(node.lineno, self.name)))

        return_type = self.types[node][0]

        if self.yields: # generator case
            # a generator has a call operator that returns the generator itself, that then behave like a regular iterator

            next_name = "__generator__"+node.name
            instanciated_next_name = "{0}{1}".format(next_name, "<{0}>".format(", ".join(formal_types)) if formal_types else "")

            next_declaration = [FunctionDeclaration( Value("return_type", "next"),
                [] ) , EmptyStatement()] #*** empty statement to force a comma ...
            next_constructors = [
                    FunctionBody(
                        FunctionDeclaration( Value("", next_name), []), Block([])
                        ),
                    FunctionBody(
                        FunctionDeclaration( Value("", next_name), [ Value( t, a ) for t,a in zip(formal_types, formal_args) ]),
                        Line("{0} {{ }}".format( ": {0}".format(", ".join("{0}({0})".format(fa) for fa in formal_args)) if formal_args else ""))
                        )
                    ]
            next_last_value = CxxBackend.generator_state_holder + "_value"
            next_iterator = [
                    FunctionBody(
                        FunctionDeclaration( Value("void", "operator++"), []), Block([Statement("{0} = next()".format(next_last_value))])),
                    FunctionBody(
                        FunctionDeclaration( Value("typename {0}::return_type".format(instanciated_next_name), "operator*"), []), Block([Statement("return {0}".format(next_last_value))])),
                    FunctionBody(
                        FunctionDeclaration( Value("{0}".format(next_name), "begin"), []), Block([Statement("++(*this) ; return *this")])),
                    FunctionBody(
                        FunctionDeclaration( Value("long", "end"), []), Block([Statement("return -1")])),
                    FunctionBody(
                        FunctionDeclaration( Value("bool", "operator!="), [Value("long","other")]), Block([Statement("return {0}!=other".format(CxxBackend.generator_state_holder))]))
                    ]
            next_signature = templatize(FunctionDeclaration(
                Value("typename {0}::return_type".format(instanciated_next_name), "{0}::next".format(instanciated_next_name)), [] ), formal_types)

            next_body = operator_body
            next_body.insert(0,Statement("switch({0}) {{ {1} }}".format( # the dispatch table at the entry point
                CxxBackend.generator_state_holder,
                " ".join("case {0}: goto {1};".format(num, where) for (num,where) in sorted(self.yields.itervalues(),key=lambda x:x[0])))))

            ctx=CachedTypeVisitor()
            next_members = [ Statement("{0} {1}".format(ft, fa)) for (ft,fa) in zip(formal_types, formal_args) ]\
                         + [ Statement("{0} {1}".format(self.types[k].generate(ctx), k.id)) for k in ldecls ]\
                         + [ Statement("{0} {1}".format(v,k)) for k,v in self.extra_declarations.iteritems() ]\
                         + [Statement("{0} {1}".format("long", CxxBackend.generator_state_holder))]\
                         + [Statement("typename {0}::return_type {1}".format(instanciated_next_name, next_last_value))]
            next_members = next_members

            extern_typedefs = [Typedef(Value(t.generate(ctx), t.name)) for t in self.types[node][1] if not t.isweak()]
            value_typedef   = [Typedef(Value(return_type.generate(ctx), "value_type"))]
            return_typedef  = [Typedef(Value(return_type.generate(ctx), "return_type"))]
            extra_typedefs  =  ctx.typedefs() + extern_typedefs + value_typedef + return_typedef

            next_struct = templatize(Struct(next_name, extra_typedefs + next_members + next_constructors + next_iterator + next_declaration), formal_types)
            next_definition = FunctionBody(next_signature, Block( next_body ))

            operator_declaration = [templatize(FunctionDeclaration( Value(instanciated_next_name, "operator()"),
                [ Value( t, a ) for t,a in zip(formal_types, formal_args) ] ), formal_types), EmptyStatement()] #*** empty statement to force a comma ...
            operator_signature = FunctionDeclaration(
                    Value(instanciated_next_name, "{0}::operator()".format(node.name)),
                    [ Value( t, a ) for t,a in zip(formal_types, formal_args ) ] )
            operator_definition = FunctionBody(
                    templatize(operator_signature, formal_types),
                    Block( [ Statement("return {0}({1})".format(instanciated_next_name, ", ".join(formal_args))) ])
                    )

            topstruct = Struct(node.name, operator_declaration)

            self.declarations.append(next_struct)
            self.definitions.append(next_definition)


        else: # regular function case
            # a function has a call operator to be called, and a default constructor to create instances
            fscope = "type{0}::".format( "<{0}>".format(", ".join(formal_types)) if formal_types else ""  )
            ffscope = "{0}::{1}".format(node.name, fscope)

            operator_declaration = [templatize(FunctionDeclaration( Value("typename "+fscope+"return_type", "operator()"),
                [ Value( t, a ) for t,a in zip(formal_types, formal_args) ] ), formal_types), EmptyStatement()] #*** empty statement to force a comma ...
            operator_signature = FunctionDeclaration(
                    Value("typename {0}return_type".format(ffscope), "{0}::operator()".format(node.name)),
                    [ Value( t, a ) for t,a in zip(formal_types, formal_args ) ] )
            ctx=CachedTypeVisitor()
            operator_local_declarations = [ Statement("{0} {1}".format(self.types[k].generate(ctx), k.id)) for k in ldecls ]\
                    + [ Statement("{0} {1}".format(v,k)) for k,v in self.extra_declarations.iteritems() ]
            dependent_typedefs = ctx.typedefs()
            operator_definition = FunctionBody(
                    templatize(operator_signature, formal_types),
                    Block( dependent_typedefs + operator_local_declarations + operator_body )
                    )

            ctx=CachedTypeVisitor()
            extra_typedefs = [Typedef(Value(t.generate(ctx), t.name)) for t in self.types[node][1] if not t.isweak()]\
                           + [Typedef(Value(return_type.generate(ctx), "return_type"))]
            extra_typedefs = ctx.typedefs() + extra_typedefs
            return_declaration = [templatize(Struct("type",extra_typedefs), formal_types)]
            topstruct = Struct(node.name, return_declaration + operator_declaration)

        self.declarations.append(topstruct)
        self.definitions.append(operator_definition)

        self.local_functions=local_functions
        return EmptyStatement()

    def visit_Return(self, node):
        if self.yields: return Statement("{0} = -1".format(CxxBackend.generator_state_holder))
        else: return ReturnStatement(self.visit(node.value))

    def visit_Delete(self, node):
        return EmptyStatement()

    def visit_Yield(self, node):
        num, label = self.yields[node]
        return "".join(n for n in Block([
            Statement("{0} = {1}".format(CxxBackend.generator_state_holder,num)),
            ReturnStatement(self.visit(node.value)),
            Statement("{0}:".format(label))
            ]).generate())

    def visit_Assign(self, node):
        if not all([isinstance(n, ast.Name) or isinstance(n, ast.Subscript) for n in node.targets]) :
            raise PythranSyntaxError("Assigning to something other than an identifier or a subscript", node)
        value = self.visit(node.value)
        targets=[self.visit(t) for t in node.targets]
        alltargets="= ".join(targets)
        return Statement("{0} = {1}".format(alltargets, value))

    def visit_AugAssign(self, node):
        value = self.visit(node.value)
        target=self.visit(node.target)
        return Statement(operator_to_lambda[type(node.op)](target, "="+value))

    def visit_Print(self, node):
        values = [ self.visit(n) for n in node.values]
        return Statement("print{0}({1})".format(
                "" if node.nl else "_nonl",
                ", ".join(values))
                )

    def visit_For(self, node):
        if not isinstance(node.target, ast.Name):
            raise PythranSyntaxError("Using something other than an identifier as loop target", node.target)
        iter = self.visit(node.iter)
        target = self.visit(node.target)
        self.break_handler.append(Block([self.visit(n) for n in node.orelse]) if node.orelse else None)
        body = [ self.visit(n) for n in node.body ]
        self.break_handler.pop()
        local_iter= "__iter{0}".format(len(self.extra_declarations))
        local_target= "__target{0}".format(len(self.extra_declarations))
        self.extra_declarations[local_iter]="decltype({0})".format(iter)
        self.extra_declarations[local_target]="decltype({0}.begin())".format(iter)
        return Block([
            Statement("{0} = {1}".format(local_iter, iter)),
            For("{0} = {1}.begin()".format(local_target, local_iter),
                "{0} != {1}.end()".format(local_target, local_iter),
                "++{0}".format(local_target),
                Block([Statement("{0} = *{1}".format(target, local_target))] + body ) )
            ])

    def visit_While(self, node):
        test = self.visit(node.test)
        self.break_handler.append(Block([self.visit(n) for n in node.orelse]) if node.orelse else None)
        body = [ self.visit(n) for n in node.body ]
        self.break_handler.pop()
        return While(test, Block(body))

    def visit_If(self, node):
        test = self.visit(node.test)
        body = [ self.visit(n) for n in node.body ]
        orelse = [ self.visit(n) for n in node.orelse ]
        return If(test, Block(body), Block(orelse) if orelse else None )

    def visit_Raise(self, node):
        type=self.visit(node.type)
        inst=self.visit(node.inst) if node.inst else None
        if inst: return Statement("throw {0}({1})".format(type, inst))
        else: return Statement("throw {0}".format(type))
            

    def visit_Assert(self, node):
        params = [ self.visit(node.msg) if node.msg else None, self.visit(node.test) ]
        return Statement("assert(({0}))".format(", ".join(p for p in params if p)))

    def visit_Import(self, node):
        return EmptyStatement() # everything is already #included

    def visit_ImportFrom(self, node):
        usings=list()
        for alias in node.names:
            if modules[node.module][alias.name]:
                usings.append("using {0}::{1}".format(node.module, alias.name))
            else:
                self.local_functions.add(alias.name)
                usings.append("using {0}::proxy::{1}".format(node.module, alias.name))

        return Statement("; ".join(usings))

    def visit_Expr(self, node):
        return Statement(self.visit(node.value))

    def visit_Pass(self, node):
        return EmptyStatement()

    def visit_Break(self, node):
        out=Statement("break")
        if self.break_handler[-1]:
            out = Block( [ self.break_handler[-1] , out ] )
        return out

    def visit_Continue(self, node):
        return Statement("continue")


    # expr
    def visit_BoolOp(self, node):
        values = [ self.visit(value) for value in node.values ]
        return reduce(operator_to_lambda[type(node.op)], values)

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        right= self.visit(node.right)
        return operator_to_lambda[type(node.op)](left,right)

    def visit_UnaryOp(self, node):
        operand = self.visit(node.operand)
        return operator_to_lambda[type(node.op)](operand)

    def visit_IfExp(self, node):
        test = self.visit(node.test)
        body = self.visit(node.body)
        orelse = self.visit(node.orelse)
        return "({0} ? {1} : {2})".format(test, body, orelse)

    def visit_List(self, node):
        if not node.elts: # empty list
            return "list()"
        else:
            elts = [ self.visit(n) for n in node.elts ]
            return "sequence< decltype({0})>({{ {1} }})".format(" + ".join(elts), ", ".join(elts))

    def visit_Tuple(self, node):
        if not node.elts: # empty tuple
            return "std::make_tuple()"
        else:
            elts = [ self.visit(n) for n in node.elts ]
            return "std::make_tuple({0})".format(", ".join(elts))

    def visit_Compare(self, node):
        left = self.visit(node.left)
        ops = [ operator_to_lambda[type(n)] for n in node.ops ]
        comparators = [ self.visit(n) for n in node.comparators ]
        all_compare = zip( [left]+comparators[:-1], ops, comparators )
        return " and ".join(op(l,r) for l,op,r in all_compare)

    def visit_Call(self, node):
        args = [ self.visit(n) for n in node.args ]
        func = self.visit(node.func)
        return "{0}({1})".format(func, ", ".join(args))

    def visit_Num(self, node):
        return str(node.n) + type_to_suffix.get(type(node.n),"")

    def visit_Str(self, node):
        return '"{0}"'.format(node.s.replace("\n",'\\n"\n"'))

    def visit_Attribute(self, node):
        value, attr = (node.value, node.attr)
        if isinstance(value, ast.Name) and value.id in modules and attr in modules[value.id]:
            if modules[value.id][attr]: return "{0}::{1}".format(value.id, attr)
            else: return "{0}::proxy::{1}()".format(value.id, attr)
        raise PythranSyntaxError("Attributes are only supported for namespaces", node)

    def visit_Subscript(self, node):
        value = self.visit(node.value)
        slice = self.visit(node.slice)
        try:
            v = constant_value(node.slice)
            return "std::get<{0}>({1})".format(v, value)
        except:
            return "{1}[{0}]".format(slice, value)

    def visit_Name(self, node):
        if node.id in self.local_declarations[-1]:
            return node.id
        elif node.id in modules["__builtins__"]:
            return "proxy::{0}()".format(node.id)
        elif node.id in self.global_declarations or node.id in self.local_functions:
            return "{0}()".format(node.id)
        else:
            return node.id


    # other

    def visit_Slice(self, node):
        lower = self.visit(node.lower) if node.lower else None
        upper = self.visit(node.upper) if node.upper else None
        step = self.visit(node.step) if node.step else None
        cv=None
        if not upper and not lower and step: # special case
            try: cv=constant_value(node.step)
            except: raise NotImplementedError("non constant step with undefined upper and lower bound in slice")
        if step:
            if not upper: upper = "std::numeric_limits<long>::max()"
        if upper:
            if not lower: lower = "0"
        if cv and cv <0: upper,lower=lower,upper

        return "slice({0})".format(", ".join( l for l in [ lower, upper, step ] if l ))

    def visit_Index(self, node):
        value = self.visit(node.value)
        return value

def cxx_backend(module_name,node):
    return CxxBackend(module_name).visit(node)
