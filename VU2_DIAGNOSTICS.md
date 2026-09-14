# Диагностика VU2 на обновлённой базе

Checkout: `87a9b4a`, branch `main`. Matching rules не изменены.

База: `.local-data/vu2.sqlite3`. Единственный report_id=1: `2026-09-01_1833_MESTO_SILY.pdf`; source_path при импорте: `/root/lto-audit/2026-09-01_1833_MESTO_SILY.pdf`. 226052 entries, decimal units. Header MESTO_SILY не используется как project filter. PDF P2 от 9 сентября в этой базе не импортирован. Проверена связь report_id с metadata базы, повторный разбор или hash PDF не выполнялся.

Системное found=0 на этой базе не воспроизводится. Причину предыдущего запуска без его базы/CLI установить нельзя. Для VU2 на трёх датах нужные пути начинаются с даты, поэтому срезать SOURCE или другой prefix не требуется.

Ниже реальные записи SQLite. relative_path — сохранённый оригинальный путь после удаления /Volumes/<tape>/ существующим parser; norm_path — сохранённый normalized path. Исходная полная строка PDF отдельно в schema не хранится. NULL size означает неизвестный размер.

## 20240129

Дата как любой component: **185 LTO entries**; из них на исторических кассетах папки: 130. XLSX rows: 130.

Первые 5 реальных XLSX rows:

| id | cassette_raw | cassette_norm | path_raw | path_norm | filename_raw | size_bytes |
| --- | --- | --- | --- | --- | --- | --- |
| 47007 | FF7472L7 | ff7472 | /20240129/CAM_A/A_0021_1D1D/A_0021_1D1D | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d | A_0021C001_240129_181231_p1D1D.mxf | NULL |
| 47008 | FF7472L7 | ff7472 | /20240129/CAM_A/A_0021_1D1D/A_0021_1D1D | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d | A_0021C002_240129_181526_p1D1D.mxf | NULL |
| 47009 | FF7472L7 | ff7472 | /20240129/CAM_A/A_0021_1D1D/A_0021_1D1D | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d | A_0021C003_240129_182719_p1D1D.mxf | NULL |
| 47010 | FF7472L7 | ff7472 | /20240129/CAM_A/A_0021_1D1D/A_0021_1D1D | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d | A_0021C004_240129_184344_p1D1D.mxf | NULL |
| 47011 | FF7472L7 | ff7472 | /20240129/CAM_A/A_0021_1D1D/A_0021_1D1D | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d | A_0021C005_240129_185032_p1D1D.mxf | NULL |

10 реальных LTO rows (3 примера других кассет и 7 исторической кассеты):

| id | tape | relative_path | norm_path | top_folder | filename | size_known | size_min_bytes | size_max_bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 164836 | FF7669 | PROXY/20240129/A_0021C001_240129_181231_p1D1D.mov | proxy/20240129/a_0021c001_240129_181231_p1d1d.mov | PROXY | A_0021C001_240129_181231_p1D1D.mov | 1 | 143345000 | 143354999 |
| 164837 | FF7669 | PROXY/20240129/A_0021C002_240129_181526_p1D1D.mov | proxy/20240129/a_0021c002_240129_181526_p1d1d.mov | PROXY | A_0021C002_240129_181526_p1D1D.mov | 1 | 955585000 | 955594999 |
| 164838 | FF7669 | PROXY/20240129/A_0021C003_240129_182719_p1D1D.mov | proxy/20240129/a_0021c003_240129_182719_p1d1d.mov | PROXY | A_0021C003_240129_182719_p1D1D.mov | 1 | 1015000000 | 1024999999 |
| 173430 | FF7472 | 20240129/CAM_A/A_0021_1D1D/A_0021_1D1D/A_0021C001_240129_181231_p1D1D.mxf | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c001_240129_181231_p1d1d.mxf | 20240129 | A_0021C001_240129_181231_p1D1D.mxf | 1 | 6705000000 | 6714999999 |
| 173432 | FF7472 | 20240129/CAM_A/A_0021_1D1D/A_0021_1D1D/A_0021C002_240129_181526_p1D1D.mxf | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c002_240129_181526_p1d1d.mxf | 20240129 | A_0021C002_240129_181526_p1D1D.mxf | 1 | 45505000000 | 45514999999 |
| 173434 | FF7472 | 20240129/CAM_A/A_0021_1D1D/A_0021_1D1D/A_0021C003_240129_182719_p1D1D.mxf | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c003_240129_182719_p1d1d.mxf | 20240129 | A_0021C003_240129_182719_p1D1D.mxf | 1 | 48515000000 | 48524999999 |
| 173436 | FF7472 | 20240129/CAM_A/A_0021_1D1D/A_0021_1D1D/A_0021C004_240129_184344_p1D1D.mxf | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c004_240129_184344_p1d1d.mxf | 20240129 | A_0021C004_240129_184344_p1D1D.mxf | 1 | 45205000000 | 45214999999 |
| 173437 | FF7472 | 20240129/CAM_A/A_0021_1D1D/A_0021_1D1D/A_0021C005_240129_185032_p1D1D.mxf | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c005_240129_185032_p1d1d.mxf | 20240129 | A_0021C005_240129_185032_p1D1D.mxf | 1 | 44075000000 | 44084999999 |
| 173438 | FF7472 | 20240129/CAM_A/A_0021_1D1D/A_0021_1D1D/A_0021C006_240129_191440_p1D1D.mxf | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c006_240129_191440_p1d1d.mxf | 20240129 | A_0021C006_240129_191440_p1D1D.mxf | 1 | 38075000000 | 38084999999 |
| 173439 | FF7472 | 20240129/CAM_A/A_0021_1D1D/A_0021_1D1D/A_0021C007_240129_192308_p1D1D.mxf | 20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c007_240129_192308_p1d1d.mxf | 20240129 | A_0021C007_240129_192308_p1D1D.mxf | 1 | 45085000000 | 45094999999 |

Распределение всех записей по tape и prefix перед датой:

| tape | prefix | entries |
| --- | --- | --- |
| FF7472 | (root) | 130 |
| FF7669 | proxy/ | 55 |

## 20240702

Дата как любой component: **46772 LTO entries**; из них на исторических кассетах папки: 106. XLSX rows: 105.

Первые 5 реальных XLSX rows:

| id | cassette_raw | cassette_norm | path_raw | path_norm | filename_raw | size_bytes |
| --- | --- | --- | --- | --- | --- | --- |
| 38669 | FF2003L7 | ff2003 | /20240702/cam_a/A_0113_1EZN | 20240702/cam_a/a_0113_1ezn | A_0113C001_240702_185431_h1EZN.mxf | NULL |
| 38670 | FF2003L7 | ff2003 | /20240702/cam_a/A_0113_1EZN | 20240702/cam_a/a_0113_1ezn | A_0113_1EZN_AVID.ale | NULL |
| 38671 | FF2003L7 | ff2003 | /20240702/cam_a/A_0113_1EZN | 20240702/cam_a/a_0113_1ezn | 0001_A_0113_1EZN_2024-07-02_214354.mhl | NULL |
| 38672 | FF2003L7 | ff2003 | /20240702/cam_a/A_0113_1EZN | 20240702/cam_a/a_0113_1ezn | A_0113C002_240702_192526_h1EZN.mxf | NULL |
| 38673 | FF2003L7 | ff2003 | /20240702/cam_a/A_0113_1EZN | 20240702/cam_a/a_0113_1ezn | A_0113C003_240702_192723_h1EZN.mxf | NULL |

10 реальных LTO rows (3 примера других кассет и 7 исторической кассеты):

| id | tape | relative_path | norm_path | top_folder | filename | size_known | size_min_bytes | size_max_bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 395 | CXZ306 | 20240702/Blackmagic/A003/A003_07021416_C001.braw | 20240702/blackmagic/a003/a003_07021416_c001.braw | 20240702 | A003_07021416_C001.braw | 1 | 280435000 | 280444999 |
| 396 | CXZ306 | 20240702/Blackmagic/A003/A003_07021419_C002.braw | 20240702/blackmagic/a003/a003_07021419_c002.braw | 20240702 | A003_07021419_C002.braw | 1 | 6385000000 | 6394999999 |
| 397 | CXZ306 | 20240702/Blackmagic/A003/A003_07021421_C003.braw | 20240702/blackmagic/a003/a003_07021421_c003.braw | 20240702 | A003_07021421_C003.braw | 1 | 4655000000 | 4664999999 |
| 164345 | FF2003 | 20240702/Phone/20240702_IMG_7620.MOV | 20240702/phone/20240702_img_7620.mov | 20240702 | 20240702_IMG_7620.MOV | 1 | 58775000 | 58784999 |
| 164346 | FF2003 | 20240702/Phone/20240702_IMG_7621.MOV | 20240702/phone/20240702_img_7621.mov | 20240702 | 20240702_IMG_7621.MOV | 1 | 76805000 | 76814999 |
| 164347 | FF2003 | 20240702/Phone/20240702_IMG_7622.MOV | 20240702/phone/20240702_img_7622.mov | 20240702 | 20240702_IMG_7622.MOV | 1 | 74325000 | 74334999 |
| 174871 | FF2003 | 20240702/cam_a/A_0105_1EZN/A_0105C001_240702_082340_h1EZN.mxf | 20240702/cam_a/a_0105_1ezn/a_0105c001_240702_082340_h1ezn.mxf | 20240702 | A_0105C001_240702_082340_h1EZN.mxf | 1 | 12295000000 | 12304999999 |
| 174873 | FF2003 | 20240702/cam_a/A_0105_1EZN/A_0105C002_240702_091246_h1EZN.mxf | 20240702/cam_a/a_0105_1ezn/a_0105c002_240702_091246_h1ezn.mxf | 20240702 | A_0105C002_240702_091246_h1EZN.mxf | 1 | 51495000000 | 51504999999 |
| 174875 | FF2003 | 20240702/cam_a/A_0105_1EZN/A_0105C003_240702_092058_h1EZN.mxf | 20240702/cam_a/a_0105_1ezn/a_0105c003_240702_092058_h1ezn.mxf | 20240702 | A_0105C003_240702_092058_h1EZN.mxf | 1 | 67665000000 | 67674999999 |
| 174876 | FF2003 | 20240702/cam_a/A_0105_1EZN/A_0105C004_240702_092839_h1EZN.mxf | 20240702/cam_a/a_0105_1ezn/a_0105c004_240702_092839_h1ezn.mxf | 20240702 | A_0105C004_240702_092839_h1EZN.mxf | 1 | 52215000000 | 52224999999 |

Распределение всех записей по tape и prefix перед датой:

| tape | prefix | entries |
| --- | --- | --- |
| CXZ306 | (root) | 46393 |
| CXZ308 | proxy/ | 273 |
| FF2003 | (root) | 106 |

## 20240905

Дата как любой component: **835 LTO entries**; из них на исторических кассетах папки: 240. XLSX rows: 240.

Первые 5 реальных XLSX rows:

| id | cassette_raw | cassette_norm | path_raw | path_norm | filename_raw | size_bytes |
| --- | --- | --- | --- | --- | --- | --- |
| 45437 | FF2018L7 | ff2018 | /20240905/PHONE Ð—Ð°Ñ\x8fÑ† | 20240905/phone ð—ð°ñ\x8fñ† | 20240905_174421.mp4 | NULL |
| 45438 | FF2018L7 | ff2018 | /20240905/PHONE Ð—Ð°Ñ\x8fÑ† | 20240905/phone ð—ð°ñ\x8fñ† | 20240905_174942.mp4 | NULL |
| 45439 | FF2018L7 | ff2018 | /20240905/PHONE Ð—Ð°Ñ\x8fÑ† | 20240905/phone ð—ð°ñ\x8fñ† | 20240905_175042.mp4 | NULL |
| 45440 | FF2018L7 | ff2018 | /20240905/PHONE Ð—Ð°Ñ\x8fÑ† | 20240905/phone ð—ð°ñ\x8fñ† | 20240905_175346.mp4 | NULL |
| 45441 | FF2018L7 | ff2018 | /20240905/PHONE Ð—Ð°Ñ\x8fÑ† | 20240905/phone ð—ð°ñ\x8fñ† | 20240905_181501.mp4 | NULL |

10 реальных LTO rows (3 примера других кассет и 7 исторической кассеты):

| id | tape | relative_path | norm_path | top_folder | filename | size_known | size_min_bytes | size_max_bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 88929 | CXZ309 | DELIVERY/From_BlackPoint/20240905/CBF_CG09_VNB01_0040_comp_v022/CBF_CG09_VNB01_0040_comp_v022.01001.exr | delivery/from_blackpoint/20240905/cbf_cg09_vnb01_0040_comp_v022/cbf_cg09_vnb01_0040_comp_v022.01001.exr | DELIVERY | CBF_CG09_VNB01_0040_comp_v022.01001.exr | 1 | 29665000 | 29674999 |
| 88930 | CXZ309 | DELIVERY/From_BlackPoint/20240905/CBF_CG09_VNB01_0040_comp_v022/CBF_CG09_VNB01_0040_comp_v022.01002.exr | delivery/from_blackpoint/20240905/cbf_cg09_vnb01_0040_comp_v022/cbf_cg09_vnb01_0040_comp_v022.01002.exr | DELIVERY | CBF_CG09_VNB01_0040_comp_v022.01002.exr | 0 | NULL | NULL |
| 88931 | CXZ309 | DELIVERY/From_BlackPoint/20240905/CBF_CG09_VNB01_0040_comp_v022/CBF_CG09_VNB01_0040_comp_v022.01003.exr | delivery/from_blackpoint/20240905/cbf_cg09_vnb01_0040_comp_v022/cbf_cg09_vnb01_0040_comp_v022.01003.exr | DELIVERY | CBF_CG09_VNB01_0040_comp_v022.01003.exr | 0 | NULL | NULL |
| 164194 | FF2018 | 20240905/GoPro/100GOPRO/GOPR0362.JPG | 20240905/gopro/100gopro/gopr0362.jpg | 20240905 | GOPR0362.JPG | 1 | 4875000 | 4884999 |
| 170588 | FF2018 | 20240905/PHONE OBSP/IMG_4853 2.MOV | 20240905/phone obsp/img_4853 2.mov | 20240905 | IMG_4853 2.MOV | 1 | 87145000 | 87154999 |
| 170589 | FF2018 | 20240905/PHONE OBSP/IMG_4854 2.MOV | 20240905/phone obsp/img_4854 2.mov | 20240905 | IMG_4854 2.MOV | 1 | 131115000 | 131124999 |
| 170591 | FF2018 | 20240905/PHONE OBSP/IMG_4855 2.MOV | 20240905/phone obsp/img_4855 2.mov | 20240905 | IMG_4855 2.MOV | 1 | 34605000 | 34614999 |
| 170592 | FF2018 | 20240905/PHONE OBSP/IMG_4856 2.MOV | 20240905/phone obsp/img_4856 2.mov | 20240905 | IMG_4856 2.MOV | 1 | 23375000 | 23384999 |
| 170593 | FF2018 | 20240905/PHONE OBSP/IMG_4857 2.MOV | 20240905/phone obsp/img_4857 2.mov | 20240905 | IMG_4857 2.MOV | 1 | 170315000 | 170324999 |
| 170595 | FF2018 | 20240905/PHONE OBSP/IMG_4858.MOV | 20240905/phone obsp/img_4858.mov | 20240905 | IMG_4858.MOV | 1 | 137345000 | 137354999 |

Распределение всех записей по tape и prefix перед датой:

| tape | prefix | entries |
| --- | --- | --- |
| CXZ307 | (root) | 131 |
| CXZ308 | for_color/ | 2 |
| CXZ308 | proxy/ | 319 |
| CXZ309 | delivery/from_blackpoint/ | 102 |
| CXZ336 | 2025-06/11-wed 32400 external-ltfs-volumes/arhiv/proxy/ | 41 |
| FF2018 | (root) | 240 |

## Кассеты

Одна и та же normalize_cassette_label используется importer для XLSX и comparator для LTO. Реальные пары:

| xlsx | yoyotta | xlsx_normalized | yoyotta_normalized |
| --- | --- | --- | --- |
| FF7472L7 | FF7472 | ff7472 | ff7472 |
| FF2003L7 | FF2003 | ff2003 | ff2003 |
| FF2018L7 | FF2018 | ff2018 | ff2018 |

CXZ306 реально есть в report; пример historical catalog: {'project_raw': 'KBD2', 'cassette_raw': 'CXZ306L7', 'cassette_norm': 'cxz306'}. Совпадение кассеты/даты другого проекта не подтверждает VU2.

## Трассировка одного идентичного файла

XLSX id=47007; LTO id=173430; report_id=1.

1. project_norm=vu2: XLSX проходит project filter.
2. XLSX Path `/20240129/CAM_A/A_0021_1D1D/A_0021_1D1D` + Filename `A_0021C001_240129_181231_p1D1D.mxf`.
3. Полный ключ обеих сторон: `20240129/cam_a/a_0021_1d1d/a_0021_1d1d/a_0021c001_240129_181231_p1d1d.mxf`.
4. `p.startswith(folder + "/")`: True для archive folder 20240129.
5. SQL `WHERE report_id=?`: LTO проходит, report_id=1.
6. `candidates = observed.get(path, [])`: возвращает LTO id=173430; candidate не отбрасывается.
7. FF7472L7 → ff7472; FF7472 → ff7472. Кассета проходит.
8. XLSX size=NULL: результат PATH_MATCH_SIZE_UNKNOWN, found увеличивается на 1.

## Реальное расхождение путей 20240905

XLSX id=45437: каталог `20240905/PHONE Ð—Ð°Ñ\x8fÑ†`, filename `20240905_174421.mp4`.
LTO id=171541: `20240905/PHONE ????/20240905_174421.mp4`; id=171542: `20240905/PHONE Заяц/20240905_174421.mp4`, оба FF2018.
Кассеты совпадают, но полные ключи различаются. На строке `candidates = observed.get(path, [])` получается []; ветка `elif not candidates` присваивает MISSING_ON_REPORT. До cassette filter кандидат не доходит.
Имена файлов использованы здесь только для диагностики; comparator не сопоставляет по basename.

## Вывод о правке

Оснований менять candidate selection или нормализацию нет: предполагаемая системная ошибка не воспроизводится. Автоматическое исправление mojibake или приведение PHONE ???? к PHONE Заяц небезопасно. Минимальное действие — повторить исходную команду на этой обновлённой базе с --report 1. Если прежний запуск использовал другую базу/report, нужны его точные аргументы и данные. Добавлены только regression tests текущего подтверждённого поведения.

## Результаты текущего comparator без fix

| folder | expected_unique_files | found | missing | conflicts | unexpected |
| --- | --- | --- | --- | --- | --- |
| 20240129 | 130 | 130 | 0 | 0 | 0 |
| 20240702 | 105 | 105 | 0 | 0 | 1 |
| 20240905 | 240 | 219 | 21 | 0 | 21 |

Полный VU2 summary (каждая папка):

| folder | expected_unique_files | found | missing | conflicts | unexpected | result |
| --- | --- | --- | --- | --- | --- | --- |
| 20240129 | 130 | 130 | 0 | 0 | 0 | CONFLICT |
| 20240131 | 67 | 67 | 0 | 0 | 0 | CONFLICT |
| 20240202 | 56 | 52 | 4 | 0 | 4 | CONFLICT |
| 20240204 | 112 | 108 | 4 | 0 | 0 | CONFLICT |
| 20240526 | 65 | 65 | 0 | 0 | 0 | CONFLICT |
| 20240611 | 99 | 99 | 0 | 0 | 0 | CONFLICT |
| 20240612 | 195 | 195 | 0 | 0 | 0 | CONFLICT |
| 20240613 | 87 | 87 | 0 | 0 | 1 | CONFLICT |
| 20240614 | 97 | 97 | 0 | 0 | 0 | CONFLICT |
| 20240615 | 89 | 0 | 89 | 0 | 0 | CONFLICT |
| 20240618 | 83 | 0 | 83 | 0 | 0 | CONFLICT |
| 20240619 | 189 | 0 | 189 | 0 | 0 | CONFLICT |
| 20240620 | 103 | 0 | 103 | 0 | 0 | CONFLICT |
| 20240624 | 215 | 215 | 0 | 0 | 0 | CONFLICT |
| 20240625 | 110 | 0 | 110 | 0 | 0 | CONFLICT |
| 20240627 | 90 | 90 | 0 | 0 | 0 | CONFLICT |
| 20240630 | 80 | 80 | 0 | 0 | 1 | CONFLICT |
| 20240701 | 92 | 92 | 0 | 0 | 1 | CONFLICT |
| 20240702 | 105 | 105 | 0 | 0 | 1 | CONFLICT |
| 20240703 | 74 | 74 | 0 | 0 | 0 | CONFLICT |
| 20240704 | 100 | 100 | 0 | 0 | 1 | CONFLICT |
| 20240705 | 74 | 74 | 0 | 0 | 0 | CONFLICT |
| 20240707 | 221 | 196 | 25 | 0 | 0 | CONFLICT |
| 20240708 | 101 | 101 | 0 | 0 | 0 | CONFLICT |
| 20240709 | 85 | 85 | 0 | 0 | 0 | CONFLICT |
| 20240714 | 106 | 106 | 0 | 0 | 0 | CONFLICT |
| 20240716 | 122 | 122 | 0 | 0 | 0 | CONFLICT |
| 20240717 | 105 | 105 | 0 | 0 | 0 | CONFLICT |
| 20240718 | 50 | 50 | 0 | 0 | 0 | CONFLICT |
| 20240719 | 95 | 95 | 0 | 0 | 0 | CONFLICT |
| 20240720 | 121 | 121 | 0 | 0 | 0 | CONFLICT |
| 20240721 | 106 | 106 | 0 | 0 | 0 | CONFLICT |
| 20240721_night | 106 | 106 | 0 | 0 | 0 | CONFLICT |
| 20240723 | 123 | 123 | 0 | 0 | 0 | CONFLICT |
| 20240724 | 196 | 196 | 0 | 0 | 0 | CONFLICT |
| 20240725 | 185 | 185 | 0 | 0 | 0 | CONFLICT |
| 20240727 | 193 | 193 | 0 | 0 | 0 | CONFLICT |
| 20240728 | 236 | 236 | 0 | 0 | 0 | CONFLICT |
| 20240730 | 227 | 227 | 0 | 0 | 0 | CONFLICT |
| 20240731 | 170 | 170 | 0 | 0 | 0 | CONFLICT |
| 20240801 | 229 | 229 | 0 | 0 | 0 | CONFLICT |
| 20240804 | 58 | 58 | 0 | 0 | 0 | CONFLICT |
| 20240805 | 161 | 161 | 0 | 0 | 0 | CONFLICT |
| 20240806 | 134 | 134 | 0 | 0 | 0 | CONFLICT |
| 20240808 | 152 | 152 | 0 | 0 | 0 | CONFLICT |
| 20240809 | 125 | 125 | 0 | 0 | 0 | CONFLICT |
| 20240812 | 160 | 160 | 0 | 0 | 0 | CONFLICT |
| 20240813 | 174 | 174 | 0 | 0 | 0 | CONFLICT |
| 20240814 | 124 | 124 | 0 | 0 | 0 | CONFLICT |
| 20240815 | 147 | 147 | 0 | 0 | 0 | CONFLICT |
| 20240816 | 376 | 376 | 0 | 0 | 0 | CONFLICT |
| 20240820 | 195 | 195 | 0 | 0 | 0 | CONFLICT |
| 20240821 | 142 | 142 | 0 | 0 | 0 | CONFLICT |
| 20240822 | 132 | 132 | 0 | 0 | 0 | CONFLICT |
| 20240823 | 185 | 185 | 0 | 0 | 0 | CONFLICT |
| 20240826 | 143 | 143 | 0 | 0 | 0 | CONFLICT |
| 20240827 | 114 | 114 | 0 | 0 | 0 | CONFLICT |
| 20240829 | 241 | 237 | 4 | 0 | 4 | CONFLICT |
| 20240830 | 119 | 118 | 1 | 0 | 1 | CONFLICT |
| 20240901 | 135 | 131 | 4 | 0 | 4 | CONFLICT |
| 20240902 | 159 | 158 | 1 | 0 | 1 | CONFLICT |
| 20240903 | 64 | 63 | 1 | 0 | 1 | CONFLICT |
| 20240904 | 95 | 88 | 7 | 0 | 7 | CONFLICT |
| 20240905 | 240 | 219 | 21 | 0 | 21 | CONFLICT |
| 20240908 | 493 | 445 | 48 | 0 | 0 | CONFLICT |
| 20240913 | 149 | 149 | 0 | 0 | 0 | CONFLICT |
| 20240915 | 107 | 107 | 0 | 0 | 0 | CONFLICT |
| 20240916 | 82 | 82 | 0 | 0 | 0 | CONFLICT |
| 20240918 | 260 | 260 | 0 | 0 | 0 | CONFLICT |
| 20240923 | 127 | 127 | 0 | 0 | 0 | CONFLICT |
| 20240925 | 103 | 103 | 0 | 0 | 0 | CONFLICT |
| 20240928 | 142 | 142 | 0 | 0 | 0 | CONFLICT |

Суммарно: {"expected_unique_files": 10127, "found": 9433, "missing": 694, "conflicts": 0, "unexpected": 48}

Все папки имеют result=CONFLICT из-за существующего глобального правила: report содержит 170506 parse issues. Это отдельно от file conflicts=0. Правило не изменялось. Найденные файлы имеют неизвестный исторический размер, поэтому found не доказывает совпадение размеров.

Parse issues по типам:

| issue_type | entries |
| --- | --- |
| sequence_file_size_unknown | 167773 |
| name_path_disagreement | 2725 |
| entry_line_layout_recovered | 8 |

## Проверки и изменённые файлы

Полный `python3 -m unittest discover`: 85 tests OK (до добавления двух regression tests: 83 OK).
`python3 -m py_compile lto_audit.py tests/test_archive_compare.py` и `git diff --check`: OK.
Изменён только tests/test_archive_compare.py; добавлен этот диагностический документ.
Production code, schema, CLI, matching rules и исходная база не изменялись.
CSV обоих прогонов и полные исходные выборки по датам находятся в игнорируемом
каталоге reports/vu2-diagnosis. Созданная ранее .venv остаётся игнорируемой;
попытка установки PyMuPDF не удалась из-за DNS, никаких новых пакетов не установлено.
