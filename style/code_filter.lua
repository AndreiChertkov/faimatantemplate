-- style/code_filter.lua
-- Препроцессор Python-файлов для блока \code.
-- Удаляет '#' перед маркером «…» (escapeinside для minted),
-- сохраняя длину строки, чтобы не нарушать выравнивание по столбцам.

local M = { tempfiles = {} }

local function strip(src, dst)
    local f = io.open(src, "rb")
    if not f then return false end
    local content = f:read("*all")
    f:close()
    -- '#' с произвольным числом пробелов до '«' меняем
    -- на пробел + те же пробелы + '«' (длина не меняется).
    content = content:gsub("#(%s*\194\171)", " %1")
    local g = io.open(dst, "wb")
    if not g then return false end
    g:write(content)
    g:close()
    return true
end

function M.process(src, dst)
    if strip(src, dst) then
        table.insert(M.tempfiles, dst)
    end
end

function M.cleanup()
    for _, p in ipairs(M.tempfiles) do
        os.remove(p)
    end
    M.tempfiles = {}
end

return M