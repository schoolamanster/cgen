cmake_minimum_required(VERSION 3.10)
project(cgen CXX)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Mirror the include layout of the original makefile.
# Headers are scattered across acl/ and bal/<area>/<subsystem>/.
include_directories(
        ${CMAKE_SOURCE_DIR}
        ${CMAKE_SOURCE_DIR}/acl
        ${CMAKE_SOURCE_DIR}/bal/anf
        ${CMAKE_SOURCE_DIR}/bal/bal
        ${CMAKE_SOURCE_DIR}/bal/cnf/cnf
        ${CMAKE_SOURCE_DIR}/bal/cnf/io
        ${CMAKE_SOURCE_DIR}/bal/cnf/encoding
        ${CMAKE_SOURCE_DIR}/bal/cnf/processor
        ${CMAKE_SOURCE_DIR}/bal/cnf/tracer
        ${CMAKE_SOURCE_DIR}/bal/encoding
        ${CMAKE_SOURCE_DIR}/bal/formula
        ${CMAKE_SOURCE_DIR}/bal/io
        ${CMAKE_SOURCE_DIR}/bal/variables
        ${CMAKE_SOURCE_DIR}/bal/utils
)

# Globs are normally discouraged in CMake (they don't auto-rerun configure on new files)
# but cgen's source set is stable, so this is acceptable here.
file(GLOB CGEN_SOURCES
        "${CMAKE_SOURCE_DIR}/*.cpp"
        "${CMAKE_SOURCE_DIR}/bal/anf/*.cpp"
        "${CMAKE_SOURCE_DIR}/bal/cnf/cnf/*.cpp"
        "${CMAKE_SOURCE_DIR}/bal/cnf/encoding/*.cpp"
        "${CMAKE_SOURCE_DIR}/bal/cnf/processor/*.cpp"
        "${CMAKE_SOURCE_DIR}/bal/formula/*.cpp"
        "${CMAKE_SOURCE_DIR}/bal/variables/*.cpp"
)

# Optimized binary: the workhorse. Matches `cgen_optimized` in the makefile.
add_executable(cgeno ${CGEN_SOURCES})

# Trace-enabled binary: defines CNF_TRACE so the -t/--trace option works.
# Slower and larger; only build when investigating solver behavior.
add_executable(cgen ${CGEN_SOURCES})
target_compile_definitions(cgen PRIVATE CNF_TRACE)
