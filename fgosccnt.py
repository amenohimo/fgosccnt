#!/usr/bin/env python3
import sys
import re
import argparse
from pathlib import Path
from collections import Counter
import csv
from enum import Enum
import itertools
import json
from operator import itemgetter
import math
import datetime
import logging

import cv2
import numpy as np
import pytesseract
from PIL import Image
from PIL.ExifTags import TAGS

import pageinfo

PROGNAME = "FGOスクショカウント"
VERSION = "0.4.0"
DEFAULT_ITEM_LANG = "jpn"  # "jpn": japanese, "eng": English

logger = logging.getLogger(__name__)


class Ordering(Enum):
    """
        ファイルの処理順序を示す定数
    """
    NOTSPECIFIED = 'notspecified'   # 指定なし
    FILENAME = 'filename'           # ファイル名
    TIMESTAMP = 'timestamp'         # 作成日時

    def __str__(self):
        return str(self.value)


basedir = Path(__file__).resolve().parent
Item_dir = basedir / Path("item/equip/")
CE_dir = basedir / Path("item/ce/")
Point_dir = basedir / Path("item/point/")
train_item = basedir / Path("item.xml")  # item stack & bonus
train_chest = basedir / Path("chest.xml")  # drop_coount (Old UI)
train_dcnt = basedir / Path("dcnt.xml")  # drop_coount (New UI)
train_card = basedir / Path("card.xml")  # card name
drop_file = basedir / Path("fgoscdata/hash_drop.json")
eventquest_dir = basedir / Path("fgoscdata/data/json/")
items_img = basedir / Path("data/misc/items_img.png")

hasher = cv2.img_hash.PHash_create()

FONTSIZE_UNDEFINED = -1
FONTSIZE_NORMAL = 0
FONTSIZE_SMALL = 1
FONTSIZE_TINY = 2
FONTSIZE_NEWSTYLE = 99
PRIORITY_CE = 9000
PRIORITY_POINT = 3000
PRIORITY_ITEM = 700
PRIORITY_GEM_MIN = 6094
PRIORITY_MAGIC_GEM_MIN = 6194
PRIORITY_SECRET_GEM_MIN = 6294
PRIORITY_PIECE_MIN = 5194
PRIORITY_REWARD_QP = 9012
ID_START = 9500000
ID_QP = 1
ID_REWARD_QP = 5
ID_GEM_MIN = 6001
ID_GEM_MAX = 6007
ID_MAGIC_GEM_MIN = 6101
ID_MAGIC_GEM_MAX = 6107
ID_SECRET_GEM_MIN = 6201
ID_SECRET_GEM_MAX = 6207
ID_PIECE_MIN = 7001
ID_MONUMENT_MAX = 7107
ID_EXP_MIN = 9700100
ID_EXP_MAX = 9707500
ID_2ZORO_DICE = 94047708
ID_3ZORO_DICE = 94047709
ID_NORTH_AMERICA = 93000500
ID_SYURENJYO = 94006800
ID_EVNET = 94000000
TIMEOUT = 15
QP_UNKNOWN = -1


class FgosccntError(Exception):
    pass


class GainedQPandDropMissMatchError(FgosccntError):
    pass


with open(drop_file, encoding='UTF-8') as f:
    drop_item = json.load(f)

# JSONファイルから各辞書を作成
item_name = {item["id"]: item["name"] for item in drop_item}
item_name_eng = {item["id"]: item["name_eng"] for item in drop_item
                 if "name_eng" in item.keys()}
item_shortname = {item["id"]: item["shortname"] for item in drop_item
                  if "shortname" in item.keys()}
item_dropPriority = {item["id"]: item["dropPriority"] for item in drop_item}
item_background = {item["id"]: item["background"] for item in drop_item
                   if "background" in item.keys()}
item_type = {item["id"]: item["type"] for item in drop_item}
dist_item = {item["phash_battle"]: item["id"] for item in drop_item
             if item["type"] == "Item" and "phash_battle" in item.keys()}
dist_ce = {item["phash"]: item["id"] for item in drop_item
           if item["type"] == "Craft Essence"}
dist_ce_narrow = {item["phash_narrow"]: item["id"] for item in drop_item
                  if item["type"] == "Craft Essence"}
dist_secret_gem = {item["id"]: item["phash_class"] for item in drop_item
                   if 6200 < item["id"] < 6208
                   and "phash_class" in item.keys()}
dist_magic_gem = {item["id"]: item["phash_class"] for item in drop_item
                  if 6100 < item["id"] < 6108 and "phash_class" in item.keys()}
dist_gem = {item["id"]: item["phash_class"] for item in drop_item
            if 6000 < item["id"] < 6008 and "phash_class" in item.keys()}
dist_exp_rarity = {item["phash_rarity"]: item["id"] for item in drop_item
                   if item["type"] == "Exp. UP"
                   and "phash_rarity" in item.keys()}
dist_exp_rarity_sold = {item["phash_rarity_sold"]: item["id"] for item
                        in drop_item if item["type"] == "Exp. UP"
                        and "phash_rarity_sold" in item.keys()}
dist_exp_rarity.update(dist_exp_rarity_sold)
dist_exp_class = {item["phash_class"]: item["id"] for item in drop_item
                  if item["type"] == "Exp. UP"
                  and "phash_class" in item.keys()}
dist_exp_class_sold = {item["phash_class_sold"]: item["id"]
                       for item in drop_item
                       if item["type"] == "Exp. UP" and "phash_class_sold"
                       in item.keys()}
dist_exp_class.update(dist_exp_class_sold)
dist_point = {item["phash_battle"]: item["id"]
              for item in drop_item
              if item["type"] == "Point" and "phash_battle" in item.keys()}

with open(drop_file, encoding='UTF-8') as f:
    drop_item = json.load(f)

freequest = []
evnetfiles = eventquest_dir.glob('**/*.json')
for evnetfile in evnetfiles:
    try:
        with open(evnetfile, encoding='UTF-8') as f:
            event = json.load(f)
            freequest = freequest + event
    except (OSError, UnicodeEncodeError) as e:
        logger.exception(e)

npz = np.load(basedir / Path('background.npz'))
hist_zero = npz["hist_zero"]
hist_gold = npz["hist_gold"]
hist_silver = npz["hist_silver"]
hist_bronze = npz["hist_bronze"]


def has_intersect(a, b):
    """
    二つの矩形の当たり判定
    隣接するのはOKとする
    """
    return max(a[0], b[0]) < min(a[2], b[2]) \
        and max(a[1], b[1]) < min(a[3], b[3])


class ScreenShot:
    """
    戦利品スクリーンショットを表すクラス
    """

    def __init__(self, args, img_rgb, svm, svm_chest, svm_dcnt, svm_card,
                 fileextention, reward_only=False):
        self.ui_type = "new"
        TRAINING_IMG_WIDTH = 1755
        threshold = 80
        try:
            self.pagenum, self.pages, self.lines = pageinfo.guess_pageinfo(img_rgb)
        except pageinfo.TooManyAreasDetectedError:
            self.pagenum, self.pages, self.lines = (-1, -1, -1)
        self.img_rgb_orig = img_rgb
        self.img_gray_orig = cv2.cvtColor(img_rgb, cv2.COLOR_BGR2GRAY)
        self.img_hsv_orig = cv2.cvtColor(img_rgb, cv2.COLOR_BGR2HSV)
        _, self.img_th_orig = cv2.threshold(self.img_gray_orig,
                                            threshold, 255, cv2.THRESH_BINARY)

        game_screen, dcnt_old, dcnt_new = self.extract_game_screen()
        if logger.isEnabledFor(logging.DEBUG):
            cv2.imwrite('game_screen.png', game_screen)

        _, width_g, _ = game_screen.shape
        wscale = (1.0 * width_g) / TRAINING_IMG_WIDTH
        resizeScale = 1 / wscale

        if resizeScale > 1:
            self.img_rgb = cv2.resize(game_screen, (0, 0),
                                      fx=resizeScale, fy=resizeScale,
                                      interpolation=cv2.INTER_CUBIC)
            if self.ui_type == "old":
                dcnt_old_rs = cv2.resize(dcnt_old, (0, 0),
                                         fx=resizeScale, fy=resizeScale,
                                         interpolation=cv2.INTER_CUBIC)
            dcnt_new_rs = cv2.resize(dcnt_new, (0, 0),
                                     fx=resizeScale, fy=resizeScale,
                                     interpolation=cv2.INTER_CUBIC)
        else:
            self.img_rgb = cv2.resize(game_screen, (0, 0),
                                      fx=resizeScale, fy=resizeScale,
                                      interpolation=cv2.INTER_AREA)
            if self.ui_type == "old":
                dcnt_old_rs = cv2.resize(dcnt_old, (0, 0),
                                         fx=resizeScale, fy=resizeScale,
                                         interpolation=cv2.INTER_AREA)
            dcnt_new_rs = cv2.resize(dcnt_new, (0, 0),
                                     fx=resizeScale, fy=resizeScale,
                                     interpolation=cv2.INTER_AREA)

        if logger.isEnabledFor(logging.DEBUG):
            cv2.imwrite('game_screen_resize.png', self.img_rgb)
            if self.ui_type == "old":
                cv2.imwrite('dcnt_old.png', dcnt_old_rs)
            cv2.imwrite('dcnt_new.png', dcnt_new_rs)

        self.img_gray = cv2.cvtColor(self.img_rgb, cv2.COLOR_BGR2GRAY)
        _, self.img_th = cv2.threshold(self.img_gray,
                                       threshold, 255, cv2.THRESH_BINARY)
        mode = self.area_select()
        logger.debug("Area Mode: %s", mode)
        self.svm = svm
        self.svm_chest = svm_chest
        self.svm_dcnt = svm_dcnt

        self.height, self.width = self.img_rgb.shape[:2]
        if self.ui_type == "old":
            self.chestnum = self.ocr_tresurechest(dcnt_old_rs)
            if self.chestnum == -1:
                self.chestnum = self.ocr_dcnt(dcnt_new_rs)
        else:
            self.chestnum = self.ocr_dcnt(dcnt_new_rs)
        # logger.debug("Total Drop (OCR): %d", self.chestnum)
        logger.debug("Total Drop (OCR): %d", self.chestnum)
        item_pts = self.img2points()
        logger.debug("item_pts:%s", item_pts)

        self.items = []
        self.current_dropPriority = PRIORITY_REWARD_QP
        if reward_only:
            # qpsplit.py で利用
            item_pts = item_pts[0:1]
        prev_item = None
        for i, pt in enumerate(item_pts):
            lx, _ = self.find_edge(self.img_th[pt[1]: pt[3],
                                               pt[0]: pt[2]], reverse=True)
            logger.debug("lx: %d", lx)
            # pt[1] + 37 for information window (new UI)
            item_img_th = self.img_th[pt[1] + 37: pt[3] - 30,
                                      pt[0] + lx: pt[2] + lx]
            if self.is_empty_box(item_img_th):
                break
            item_img_rgb = self.img_rgb[pt[1]:  pt[3],
                                        pt[0] + lx:  pt[2] + lx]
            item_img_gray = self.img_gray[pt[1]: pt[3],
                                          pt[0] + lx: pt[2] + lx]
            if logger.isEnabledFor(logging.DEBUG):
                cv2.imwrite('item' + str(i) + '.png', item_img_rgb)
            dropitem = Item(args, i, prev_item, item_img_rgb, item_img_gray,
                            svm, svm_card, fileextention,
                            self.current_dropPriority, mode)
            if dropitem.id == -1:
                break
            self.current_dropPriority = item_dropPriority[dropitem.id]
            self.items.append(dropitem)
            prev_item = dropitem

        self.itemlist = self.makeitemlist()
        try:
            self.total_qp = self.get_qp(mode)
            self.qp_gained = self.get_qp_gained(mode)
        except Exception as e:
            self.total_qp = -1
            self.qp_gained = -1
            logger.warning("QP detection fails")
            logger.exception(e)
        if self.qp_gained > 0 and len(self.itemlist) == 0:
            raise GainedQPandDropMissMatchError
        self.pagenum, self.pages, self.lines = self.correct_pageinfo()
        if not reward_only:
            self.check_page_mismatch()

    def check_page_mismatch(self):
        count_miss = False
        if self.pages == 1:
            if self.chestnum + 1 != len(self.itemlist):
                count_miss = True
        elif self.pages == 2:
            if not 21 <= self.chestnum <= 41:
                count_miss = True
            if self.pagenum == 2:
                item_count = self.chestnum - 20 + (6 - self.lines)*7
                if item_count != len(self.itemlist):
                    count_miss = True
        elif self.pages == 3:
            if not 42 <= self.chestnum <= 62:
                count_miss = True
            if self.pagenum == 3:
                item_count = self.chestnum - 41 + (9 - self.lines)*7
                if item_count != len(self.itemlist):
                    count_miss = True
        if count_miss:
            logger.warning("drops_count is a mismatch:")
            logger.warning("drops_count = %d", self.chestnum)
            logger.warning("drops_found = %d", len(self.itemlist))

    def detect_scroll_bar(self):
        '''
        Modified from determine_scroll_position()
        '''
        width = self.img_rgb.shape[1]
        topleft = (width - 90, 81)
        bottomright = (width, 2 + 753)

        if logger.isEnabledFor(logging.DEBUG):
            img_copy = self.img_rgb.copy()
            cv2.rectangle(img_copy, topleft, bottomright, (0, 0, 255), 3)
            cv2.imwrite("./scroll_bar_selected2.jpg", img_copy)

        gray_image = self.img_gray[
                                   topleft[1]: bottomright[1],
                                   topleft[0]: bottomright[0]
                                   ]
        _, binary = cv2.threshold(gray_image, 200, 255, cv2.THRESH_BINARY)
        if logger.isEnabledFor(logging.DEBUG):
            cv2.imwrite("scroll_bar_binary2.png", binary)
        contours = cv2.findContours(
                                    binary,
                                    cv2.RETR_LIST,
                                    cv2.CHAIN_APPROX_NONE
                                    )[0]
        pts = []
        for cnt in contours:
            ret = cv2.boundingRect(cnt)
            pt = [ret[0], ret[1], ret[0] + ret[2], ret[1] + ret[3]]
            if ret[3] > 10:
                pts.append(pt)
        if len(pts) == 0:
            logger.debug("Can't find scroll bar")
            return -1, -1
        elif len(pts) > 1:
            logger.warning("Too many objects.")
            return -1, -1
        else:
            return pt[1], pt[3] - pt[1]

    def valid_pageinfo(self):
        '''
        Checking the content of pageinfo and correcting it when it fails
        '''
        if self.pagenum == -1 or self.pages == -1 or self.lines == -1:
            return False
        elif self.itemlist[0]["id"] != ID_REWARD_QP and self.pagenum == 1:
            return False
        elif self.chestnum != -1 and self.pagenum != 1 \
                and self.lines != int(self.chestnum/7) + 1:
            return False
        return True

    def correct_pageinfo(self):
        if self.valid_pageinfo() is False:
            logger.warning("pageinfo validation failed")
            asr_y, actual_height = self.detect_scroll_bar()
            if asr_y == -1 or actual_height == -1:
                return 1, 1, 0
            entire_height = 649
            esr_y = 17
            pagenum = pageinfo.guess_pagenum(asr_y, esr_y, entire_height)
            pages = pageinfo.guess_pages(actual_height, entire_height)
            lines = pageinfo.guess_lines(actual_height, entire_height)
            return pagenum, pages, lines
        else:
            return self.pagenum, self.pages, self.lines

    def calc_black_whiteArea(self, bw_image):
        image_size = bw_image.size
        whitePixels = cv2.countNonZero(bw_image)

        whiteAreaRatio = (whitePixels / image_size) * 100  # [%]

        return whiteAreaRatio

    def is_empty_box(self, img_th):
        """
        アイテムボックスにアイテムが無いことを判別する
        """
        if self.calc_black_whiteArea(img_th) < 1:
            return True
        return False

    def get_qp_from_text(self, text):
        """
        capy-drop-parser から流用
        """
        qp = 0
        power = 1
        # re matches left to right so reverse the list
        # to process lower orders of magnitude first.
        for match in re.findall("[0-9]+", text)[::-1]:
            qp += int(match) * power
            power *= 1000

        return qp

    def extract_text_from_image(self, image):
        """
        capy-drop-parser から流用
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, qp_image = cv2.threshold(gray, 65, 255, cv2.THRESH_BINARY_INV)

        return pytesseract.image_to_string(
            qp_image,
            config="-l eng --oem 1 --psm 7 -c tessedit_char_whitelist=+,0123456789",
        )

    def get_qp(self, mode):
        """
        capy-drop-parser から流用
        tesseract-OCR is quite slow and changed to use SVM
        """
        use_tesseract = False
        pt = pageinfo.detect_qp_region(self.img_rgb_orig, mode)
        logger.debug('pt from pageinfo: %s', pt)
        if pt is None:
            use_tesseract = True

        qp_total = -1
        if use_tesseract is False:  # use SVM
            im_th = cv2.bitwise_not(
                self.img_th_orig[pt[0][1]: pt[1][1], pt[0][0]: pt[1][0]]
            )
            qp_total = self.ocr_text(im_th)
        if use_tesseract or qp_total == -1:
            if self.ui_type == "old":
                pt = ((288, 948), (838, 1024))
            else:
                pt = ((288, 838), (838, 914))
            logger.debug('Use tesseract')
            qp_total_text = self.extract_text_from_image(
                self.img_rgb[pt[0][1]: pt[1][1], pt[0][0]: pt[1][0]]
            )
            logger.debug('qp_total_text from text: %s', qp_total_text)
            qp_total = self.get_qp_from_text(qp_total_text)

        logger.debug('qp_total from text: %s', qp_total)
        if len(str(qp_total)) > 9:
            logger.warning(
                "qp_total exceeds the system's maximum: %s", qp_total
            )
        if qp_total == 0:
            return QP_UNKNOWN

        return qp_total

    def get_qp_gained(self, mode):
        use_tesseract = False
        bounds = pageinfo.detect_qp_region(self.img_rgb_orig, mode)
        if bounds is None:
            # fall back on hardcoded bound
            if self.ui_type == "old":
                bounds = ((398, 858), (948, 934))
            else:
                bounds = ((398, 748), (948, 824))
            use_tesseract = True
        else:
            # Detecting the QP box with different shading is "easy", while detecting the absence of it
            # for the gain QP amount is hard. However, the 2 values have the same font and thus roughly
            # the same height (please NA...). You can consider them to be 2 same-sized boxes on top of
            # each other.
            (topleft, bottomright) = bounds
            height = bottomright[1] - topleft[1]
            topleft = (topleft[0], topleft[1] - height + int(height*0.12))
            bottomright = (bottomright[0], bottomright[1] - height)
            bounds = (topleft, bottomright)

        logger.debug('Gained QP bounds: %s', bounds)
        if logger.isEnabledFor(logging.DEBUG):
            img_copy = self.img_rgb.copy()
            cv2.rectangle(img_copy, bounds[0], bounds[1], (0, 0, 255), 3)
            cv2.imwrite("./qp_gain_detection.jpg", img_copy)

        qp_gain = -1
        if use_tesseract is False:
            im_th = cv2.bitwise_not(
                self.img_th_orig[topleft[1]: bottomright[1],
                                 topleft[0]: bottomright[0]]
            )
            qp_gain = self.ocr_text(im_th)
        if use_tesseract or qp_gain == -1:
            logger.debug('Use tesseract')
            (topleft, bottomright) = bounds
            qp_gain_text = self.extract_text_from_image(
                self.img_rgb[topleft[1]: bottomright[1],
                             topleft[0]: bottomright[0]]
            )
            qp_gain = self.get_qp_from_text(qp_gain_text)
        logger.debug('qp from text: %s', qp_gain)
        if qp_gain == 0:
            qp_gain = QP_UNKNOWN

        return qp_gain

    def find_edge(self, img_th, reverse=False):
        """
        直線検出で検出されなかったフチ幅を検出
        """
        edge_width = 4
        _, width = img_th.shape[:2]
        target_color = 255 if reverse else 0
        for i in range(edge_width):
            img_th_x = img_th[:, i:i + 1]
            # ヒストグラムを計算
            hist = cv2.calcHist([img_th_x], [0], None, [256], [0, 256])
            # 最小値・最大値・最小値の位置・最大値の位置を取得
            _, _, _, maxLoc = cv2.minMaxLoc(hist)
            if maxLoc[1] == target_color:
                break
        lx = i
        for j in range(edge_width):
            img_th_x = img_th[:, width - j - 1: width - j]
            # ヒストグラムを計算
            hist = cv2.calcHist([img_th_x], [0], None, [256], [0, 256])
            # 最小値・最大値・最小値の位置・最大値の位置を取得
            _, _, _, maxLoc = cv2.minMaxLoc(hist)
            if maxLoc[1] == 0:
                break
        rx = j

        return lx, rx

    def find_notch(self, img_hsv):
        """
        直線検出で検出されなかったフチ幅を検出
        """
        edge_width = 150
        threshold = 0.65

        height, width = img_hsv.shape[:2]
        target_color = 0
        for i in range(edge_width):
            img_hsv_x = img_hsv[:, i:i + 1]
            # ヒストグラムを計算
            hist = cv2.calcHist([img_hsv_x], [0], None, [256], [0, 256])
            # 最小値・最大値・最小値の位置・最大値の位置を取得
            _, maxVal, _, maxLoc = cv2.minMaxLoc(hist)
            if not (maxLoc[1] == target_color and maxVal > height * threshold):
                break
        lx = i
        for j in range(edge_width):
            img_hsv_x = img_hsv[:, width - j - 1: width - j]
            # ヒストグラムを計算
            hist = cv2.calcHist([img_hsv_x], [0], None, [256], [0, 256])
            # 最小値・最大値・最小値の位置・最大値の位置を取得
            _, maxVal, _, maxLoc = cv2.minMaxLoc(hist)
            if not (maxLoc[1] == target_color and maxVal > height * threshold):
                break
        rx = j

        return lx, rx

    def extract_game_screen(self):
        """
        1. Make cutting image using edge and line detection
        2. Correcting to be a gamescreen from cutting image
        """
        upper_lower_blue_border = False  # For New UI
        # 1. Edge detection
        height, width = self.img_gray_orig.shape[:2]
        canny_img = cv2.Canny(self.img_gray_orig, 80, 80)

        if logger.isEnabledFor(logging.DEBUG):
            cv2.imwrite("canny_img.png", canny_img)

        # 2. Line detection
        # In the case where minLineLength is too short,
        # it catches the line of the item.
        lines = cv2.HoughLinesP(canny_img, rho=1, theta=np.pi/2,
                                threshold=80, minLineLength=int(height/5),
                                maxLineGap=10)

        left_x = upper_y = b_line_y = 0
        right_x = width
        bottom_y = height
        for line in lines:
            x1, y1, x2, y2 = line[0]
            # Detect Left line
            if x1 == x2 and x1 < width/2 and abs(y2 - y1) > height/2:
                if left_x < x1:
                    left_x = x1
        # Define Center
        lx, rx = self.find_notch(self.img_hsv_orig)
        logger.debug("notch_lx = %d, notch_rx = %d", lx, rx)
        center = int((width - lx - rx)/2) + lx
        # 旧UIの破線y座標位置以上にする
        # 一行目のアイテムの下部にできる直線さけ
        # BlueStacksのスクショをメニューバーとともにスクショにとる人のため上部直線ではとらないことでマージンにする
        # 一行目のアイテムの上部に直線ができるケースがあったら改めて考える
        if width/height > 16/9.01:
            b_line_y_border = 300 * height / 1152
        else:
            b_line_y_border = 300 * width / 2048 + int((height - width * 9/16)/2)
        for line in lines:
            x1, y1, x2, y2 = line[0]
            # Detect Broken line
            if y1 == y2 and y1 < b_line_y_border \
               and ((x1 < left_x + 200 and x2 > left_x) \
               or (center + (center - left_x) - 200 < x2 < center + (center - left_x))):
                if b_line_y < y1:
                    b_line_y = y1
            # Detect Upper line
#            if y1 == y2 and y1 < height/2 and x1 < left_x + 15:
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if y1 == y2 and x1 < left_x + 200 \
               and x2 > left_x + 200 and y1 < b_line_y - 30:
                if upper_y < y1:
                    upper_y = y1
        logger.debug("left_x: %d", left_x)
        logger.debug("b_line_y: %d", b_line_y)
        logger.debug("upper_y: %d", upper_y)


        # Detect Right line
        # Avoid catching the line of the scroll bar
        if logger.isEnabledFor(logging.DEBUG):
            line_img = self.img_rgb_orig.copy()

        for line in lines:
            x1, y1, x2, y2 = line[0]
            if logger.isEnabledFor(logging.DEBUG):
                line_img = cv2.line(line_img, (x1, y1), (x2, y2),
                                    (0, 0, 255), 1)
                cv2.imwrite("line_img.png", line_img)
            # if x1 == x2 and x1 > width*3/4 and (y1 < b_line_y or y2 < b_line_y):
            if x1 == x2 and x1 >= center + (center - left_x) - 5 and (y1 < b_line_y or y2 < b_line_y):
                if right_x > x1:
                    right_x = x1
        if right_x > width - 50:
            logger.warning("right_x detection failed.")
            # Redefine right_x from the pseudo_bottom_y
            pseudo_bottom_y = height
            for line in lines:
                x1, y1, x2, y2 = line[0]
                if y1 == y2 and y1 > height/2 and (x1 < width/2):
                    if pseudo_bottom_y > y1:
                        pseudo_bottom_y = y1
            logger.debug("pseudo_bottom_y: %d", pseudo_bottom_y)
            for line in lines:
                x1, y1, x2, y2 = line[0]
                if x1 == x2 and x1 > width*3/4 \
                   and (y1 > pseudo_bottom_y or y2 > pseudo_bottom_y):
                    if right_x > x1:
                        right_x = x1
        logger.debug("right_x: %d", right_x)

        # Detect Bottom line
        # Changed the underline of cut image to use the top of Next button.
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if y1 == y2 and y1 > height/2 and (x1 < right_x and x2 > right_x):
                if bottom_y > y1:
                    bottom_y = y1
        logger.debug("bottom_y: %d", bottom_y)
        logger.debug("height: %d", height)
        if width/height > 16/9.01:
            # 上下青枠以外
            logger.debug("no border or side blue border")
            if (bottom_y - upper_y)/height >= 0.765:
                if upper_y/(height - bottom_y) < 0.3:
                    logger.debug("New UI")
                    self.ui_type = "new"
                    if width/height < 16/8.99:
                        upper_lower_blue_border = True
                else:
                    logger.debug("Old UI")
                    self.ui_type = "old"
            else:
                # iPhone X type blue border
                logger.debug("Old UI")
                self.ui_type = "old"
        else:
            # 上下青枠
            logger.debug("top & bottom blue border")
            game_height = width * 9 / 16
            bh = (height - game_height)/2  # blue border height
            upper_h = upper_y - bh
            bottom_h = height - bottom_y - bh
            if upper_h/bottom_h < 0.3:
                logger.debug("New UI")
                self.ui_type = "new"
                upper_lower_blue_border = True
            else:
                logger.debug("Old UI")
                self.ui_type = "old"
        TEMPLATE_WIDTH = 1902 - 146
        TEMPLATE_HEIGHT = 1158 - 234
        scale = TEMPLATE_WIDTH / TEMPLATE_HEIGHT
        lack_of_height = (right_x - left_x)/(bottom_y - upper_y + 10) > scale
        if bottom_y == height or lack_of_height:
            bottom_y = upper_y + int((right_x - left_x) / scale)
            logger.warning("bottom line detection failed")
            logger.debug("redefine bottom_y: %s", bottom_y)

        if logger.isEnabledFor(logging.DEBUG):
            tmpimg = self.img_rgb_orig[upper_y: bottom_y, left_x: right_x]
            cv2.imwrite("cutting_img.png", tmpimg)
        # 内側の直線をとれなかったときのために補正する
        thimg = self.img_th_orig[upper_y: bottom_y, left_x: right_x]
        lx, rx = self.find_edge(thimg)
        left_x = left_x + lx
        right_x = right_x - rx

        # Correcting to be a gamescreen
        # Actual iPad (2048x1536) measurements
        scale = bottom_y - upper_y
        logger.debug("scale: %d", scale)
        # upper_y = upper_y - int(79*scale/847)
        bottom_y = bottom_y + int(124*scale/924)
        logger.debug(bottom_y)
        game_screen = self.img_rgb_orig[upper_y: bottom_y, left_x: right_x]
        dcnt_old = None
        if self.ui_type == "old":
            left_dxo = left_x + int(1446*scale/924)
            right_dxo = left_dxo + int(53*scale/924)
            upper_dyo = upper_y - int(81*scale/924)
            bottom_dyo = upper_dyo + int(37*scale/924)
            dcnt_old = self.img_rgb_orig[upper_dyo: bottom_dyo,
                                         left_dxo: right_dxo]
        if upper_lower_blue_border:
            left_dx = left_x + int(1463*scale/924)
            right_dx = left_dx + int(67*scale/924)
        else:
            left_dx = left_x + int(1400*scale/924)
            right_dx = left_dx + int(305*scale/924)
        upper_dy = upper_y - int(20*scale/924)
        bottom_dy = upper_dy + int(37*scale/924)
        # bottom_dy = upper_dy + int(41*scale/847)

        logger.debug("left_dx: %d", left_dx)
        logger.debug("right_dx: %d", right_dx)
        logger.debug("upper_dy: %d", upper_dy)
        logger.debug("bottom_dy: %d", bottom_dy)
        dcnt_new = self.img_rgb_orig[upper_dy: bottom_dy,
                                     left_dx: right_dx]

        return game_screen, dcnt_old, dcnt_new

    def area_select(self):
        """
        FGOアプリの地域を選択
        'na', 'jp'に対応

        'items_img.png' とのオブジェクトマッチングで判定
        """
        img_gray = self.img_gray[0:100, 0:500]
        template = imread(items_img, 0)
        res = cv2.matchTemplate(
                                img_gray,
                                template,
                                cv2.TM_CCOEFF_NORMED
                                )
        threshold = 0.9
        loc = np.where(res >= threshold)
        for pt in zip(*loc[::-1]):
            return 'na'
            break
        return 'jp'

    def makeitemlist(self):
        """
        アイテムを出力
        """
        itemlist = []
        for item in self.items:
            tmp = {}
            tmp['id'] = item.id
            tmp['name'] = item.name
            tmp['dropPriority'] = item_dropPriority[item.id]
            tmp['dropnum'] = int(item.dropnum[1:])
            tmp['bonus'] = item.bonus
            tmp['category'] = item.category
            itemlist.append(tmp)
        return itemlist

    def ocr_text(self, im_th):
        h, w = im_th.shape[:2]
        # 物体検出
        im_th = cv2.bitwise_not(im_th)
        contours = cv2.findContours(im_th,
                                    cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)[0]
        item_pts = []
        for cnt in contours:
            ret = cv2.boundingRect(cnt)
            area = cv2.contourArea(cnt)
            pt = [ret[0], ret[1], ret[0] + ret[2], ret[1] + ret[3]]
            if ret[2] < int(w/2) and area > 80 and ret[1] < h/2 \
                    and 0.3 < ret[2]/ret[3] < 0.85 and ret[3] > h * 0.45:
                flag = False
                for p in item_pts:
                    if has_intersect(p, pt):
                        # どちらかを消す
                        p_area = (p[2]-p[0])*(p[3]-p[1])
                        pt_area = ret[2]*ret[3]
                        if p_area < pt_area:
                            item_pts.remove(p)
                        else:
                            flag = True

                if flag is False:
                    item_pts.append(pt)

        if len(item_pts) == 0:
            # Recognizing Failure
            return -1
        item_pts.sort()
        if len(item_pts) > 9:
            # QP may be misrecognizing the 10th digit or more, so cut it
            item_pts = item_pts[1:]
        logger.debug("ocr item_pts: %s", item_pts)
        logger.debug("ドロップ桁数(OCR): %d", len(item_pts))

        # Hog特徴のパラメータ
        win_size = (120, 60)
        block_size = (16, 16)
        block_stride = (4, 4)
        cell_size = (4, 4)
        bins = 9

        res = ""
        for pt in item_pts:
            test = []

            if pt[0] == 0:
                tmpimg = im_th[pt[1]:pt[3], pt[0]:pt[2]+1]
            else:
                tmpimg = im_th[pt[1]:pt[3], pt[0]-1:pt[2]+1]
            tmpimg = cv2.resize(tmpimg, (win_size))
            hog = cv2.HOGDescriptor(win_size, block_size,
                                    block_stride, cell_size, bins)
            test.append(hog.compute(tmpimg))  # 特徴量の格納
            test = np.array(test)

            pred = self.svm_chest.predict(test)
            res = res + str(int(pred[1][0][0]))

        return int(res)

    def ocr_tresurechest(self, drop_count_img):
        """
        宝箱数をOCRする関数
        """
        threshold = 80
        img_gray = cv2.cvtColor(drop_count_img, cv2.COLOR_BGR2GRAY)
        _, img_num = cv2.threshold(img_gray,
                                   threshold, 255, cv2.THRESH_BINARY)
        im_th = cv2.bitwise_not(img_num)
        h, w = im_th.shape[:2]

        # 情報ウィンドウが数字とかぶった部分を除去する
        for y in range(h):
            im_th[y, 0] = 255
        for x in range(w):  # ドロップ数7のときバグる対策 #54
            im_th[0, x] = 255
        return self.ocr_text(im_th)

    def pred_dcnt(self, img):
        """
        for JP new UI
        """
        # Hog特徴のパラメータ
        win_size = (120, 60)
        block_size = (16, 16)
        block_stride = (4, 4)
        cell_size = (4, 4)
        bins = 9
        char = []

        tmpimg = cv2.resize(img, (win_size))
        hog = cv2.HOGDescriptor(win_size, block_size,
                                block_stride, cell_size, bins)
        char.append(hog.compute(tmpimg))  # 特徴量の格納
        char = np.array(char)

        pred = self.svm_dcnt.predict(char)
        res = str(int(pred[1][0][0]))

        return int(res)

    def img2num(self, img, img_th, pts, char_w, end):
        """実際より小さく切り抜かれた数字画像を補正して認識させる

        """
        height, width = img.shape[:2]
        c_center = int(pts[0] + (pts[2] - pts[0])/2)
        # newimg = img[:, item_pts[-1][0]-1:item_pts[-1][2]+1]
        newimg = img[:, max(int(c_center - char_w/2), 0):min(int(c_center + char_w/2), width)]

        threshold2 = 10
        ret, newimg_th = cv2.threshold(newimg,
                                       threshold2,
                                       255,
                                       cv2.THRESH_BINARY)
        # 上部はもとのやつを上書き
        # for w in range(item_pts[-1][2] - item_pts[-1][0] + 2):
        for w in range(min(int(c_center + char_w/2), width) - max(int(c_center - char_w/2), 0)):
            for h in range(end):
                newimg_th[h, w] = img_th[h, w + int(c_center - char_w/2)]
        #        newimg_th[h, w] = img_th[h, w + item_pts[-1][0]]
            newimg_th[height - 1, w] = 0
            newimg_th[height - 2, w] = 0
            newimg_th[height - 3, w] = 0

        res = self.pred_dcnt(newimg_th)
        return res

    def ocr_dcnt(self, drop_count_img):
        """
        ocr drop_count (for New UI)
        """
        char_w = 28
        threshold = 80
        kernel = np.ones((4, 4), np.uint8)
        img = cv2.cvtColor(drop_count_img, cv2.COLOR_BGR2GRAY)
        _, img_th = cv2.threshold(img, threshold, 255, cv2.THRESH_BINARY)
        img_th = cv2.dilate(img_th, kernel, iterations=1)
        height, width = img_th.shape[:2]

        end = -1
        for i in range(height):
            if end == -1 and img_th[height - i - 1, width - 1] == 255:
                end = height - i
                break
        start = end - 7

        for j in range(width):
            for k in range(end - start):
                img_th[start + k, j] = 0

        contours = cv2.findContours(img_th,
                                    cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)[0]
        item_pts = []
        for cnt in contours:
            ret = cv2.boundingRect(cnt)
            pt = [ret[0], ret[1], ret[0] + ret[2], ret[1] + ret[3]]
            if ret[1] > 0 and ret[3] > 8 and ret[1] + ret[3] == start \
               and 12 < ret[2] < char_w + 4 and ret[0] + ret[2] != width:
                item_pts.append(pt)

        if len(item_pts) == 0:
            return -1
        item_pts.sort()

        res = self.img2num(img, img_th, item_pts[-1], char_w, end)
        if len(item_pts) >= 2:
            if item_pts[-1][0] - item_pts[-2][2] < char_w / (2 / 3):
                res2 = self.img2num(img, img_th, item_pts[-2], char_w, end)
                res = res2 * 10 + res

        return res

    def calc_offset(self, pts, std_pts, margin_x):
        """
        オフセットを反映
        """
        if len(pts) == 0:
            return std_pts
        # Y列でソート
        pts.sort(key=lambda x: x[1])
        if len(pts) > 1:  # fix #107
            if (pts[1][3] - pts[1][1]) - (pts[0][3] - pts[0][1]) > 0:
                pts = pts[1:]
        # Offsetを算出
        offset_x = pts[0][0] - margin_x
        offset_y = pts[0][1] - std_pts[0][1]
        if offset_y > (std_pts[7][3] - std_pts[7][1])*2:
            # これ以上になったら三行目の座標と判断
            offset_y = pts[0][1] - std_pts[14][1]
        elif offset_y > 30:
            # これ以上になったら二行目の座標と判断
            offset_y = pts[0][1] - std_pts[7][1]

        # Offset を反映
        item_pts = []
        for pt in std_pts:
            ptl = list(pt)
            ptl[0] = ptl[0] + offset_x
            ptl[1] = ptl[1] + offset_y
            ptl[3] = ptl[3] + offset_y
            ptl[2] = ptl[2] + offset_x
            item_pts.append(ptl)
        return item_pts

    def img2points(self):
        """
        戦利品左一列のY座標を求めて標準座標とのずれを補正して座標を出力する
        """
        std_pts = self.booty_pts()

        row_size = 7  # アイテム表示最大列
        col_size = 3  # アイテム表示最大行
        margin_x = 15
        area_size_lower = 37000  # アイテム枠の面積の最小値
        img_1strow = self.img_th[0:self.height,
                                 std_pts[0][0] - margin_x:
                                 std_pts[0][2] + margin_x]
        # kernel = np.ones((5,1),np.uint8)
        # img_1strow = cv2.dilate(img_1strow,kernel,iterations = 1)

        # 輪郭を抽出
        contours = cv2.findContours(img_1strow, cv2.RETR_TREE,
                                    cv2.CHAIN_APPROX_SIMPLE)[0]

        leftcell_pts = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area > area_size_lower \
               and area < self.height * self.width / (row_size * col_size):
                epsilon = 0.01*cv2.arcLength(cnt, True)
                approx = cv2.approxPolyDP(cnt, epsilon, True)
                if 4 <= len(approx) <= 6:  # 六角形のみ認識
                    ret = cv2.boundingRect(cnt)
                    if ret[1] > self.height * 0.15 - 101 \
                       and ret[1] + ret[3] < self.height * 0.76 - 101:
                        # 小数の数値はだいたいの実測
                        pts = [ret[0], ret[1],
                               ret[0] + ret[2], ret[1] + ret[3]]
                        leftcell_pts.append(pts)
        item_pts = self.calc_offset(leftcell_pts, std_pts, margin_x)
        logger.debug("leftcell_pts: %s", leftcell_pts)

        return item_pts

    def booty_pts(self):
        """
        戦利品が出現する21の座標 [left, top, right, bottom]
        解像度別に設定
        """
        criteria_left = 102
        criteria_top = 99
        item_width = 188
        item_height = 206
        margin_width = 32
        margin_height = 21
        pts = generate_booty_pts(criteria_left, criteria_top,
                                 item_width, item_height,
                                 margin_width, margin_height)
        return pts


def generate_booty_pts(criteria_left, criteria_top, item_width, item_height,
                       margin_width, margin_height):
    """
        ScreenShot#booty_pts() が返すべき座標リストを生成する。
        全戦利品画像が等間隔に並んでいることを仮定している。

        criteria_left ... 左上にある戦利品の left 座標
        criteria_top ... 左上にある戦利品の top 座標
        item_width ... 戦利品画像の width
        item_height ... 戦利品画像の height
        margin_width ... 戦利品画像間の width
        margin_height ... 戦利品画像間の height
    """
    pts = []
    current = (criteria_left, criteria_top, criteria_left + item_width,
               criteria_top + item_height)
    for j in range(3):
        # top, bottom の y座標を計算
        current_top = criteria_top + (item_height + margin_height) * j
        current_bottom = current_top + item_height
        # x座標を左端に固定
        current = (criteria_left, current_top,
                   criteria_left + item_width, current_bottom)
        for i in range(7):
            # y座標を固定したままx座標をスライドさせていく
            current_left = criteria_left + (item_width + margin_width) * i
            current_right = current_left + item_width
            current = (current_left, current_top,
                       current_right, current_bottom)
            pts.append(current)
    return pts


class Item:
    def __init__(self, args, pos, prev_item, img_rgb, img_gray, svm, svm_card,
                 fileextention, current_dropPriority, mode='jp'):
        self.position = pos
        self.prev_item = prev_item
        self.img_rgb = img_rgb
        self.img_gray = img_gray
        self.img_hsv = cv2.cvtColor(img_rgb, cv2.COLOR_BGR2HSV)
        _, img_th = cv2.threshold(self.img_gray, 174, 255, cv2.THRESH_BINARY)
        self.img_th = cv2.bitwise_not(img_th)
        self.fileextention = fileextention
        self.dropnum_cache = []
        self.margin_left = 5

        self.height, self.width = img_rgb.shape[:2]
        logger.debug("pos: %d", pos)
        self.identify_item(args, prev_item, svm_card,
                           current_dropPriority)
        if self.id == -1:
            return
        logger.debug("id: %d", self.id)
        logger.debug("background: %s", self.background)
        logger.debug("dropPriority: %s", item_dropPriority[self.id])
        logger.debug("Category: %s", self.category)
        logger.debug("Name: %s", self.name)

        self.svm = svm
        self.bonus = ""
        if self.category != "Craft Essence" and self.category != "Exp. UP":
            self.ocr_digit(mode)
        else:
            self.dropnum = "x1"
        logger.debug("Bonus: %s", self.bonus)
        logger.debug("Stack: %s", self.dropnum)

    def identify_item(self, args, prev_item, svm_card,
                      current_dropPriority):
        self.background = classify_background(self.img_rgb)
        self.hash_item = compute_hash(self.img_rgb)  # 画像の距離
        if prev_item is not None:
            # [Requirements for Caching]
            # 1. previous item is not a reward QP.
            # 2. Same background as the previous item
            # 3. Not (similarity is close) dice, gem or EXP
            if prev_item.id != ID_REWARD_QP \
                and prev_item.background == self.background \
                and not (ID_GEM_MIN <= prev_item.id <= ID_SECRET_GEM_MAX or
                         ID_2ZORO_DICE <= prev_item.id <= ID_3ZORO_DICE or
                         ID_EXP_MIN <= prev_item.id <= ID_EXP_MAX):
                d = hasher.compare(self.hash_item, prev_item.hash_item)
                if d <= 4:
                    self.category = prev_item.category
                    self.id = prev_item.id
                    self.name = prev_item.name
                    return
        self.category = self.classify_category(svm_card)
        self.id = self.classify_card(self.img_rgb, current_dropPriority)
        if args.lang == "jpn":
            self.name = item_name[self.id]
        else:
            if self.id in item_name_eng.keys():
                self.name = item_name_eng[self.id]
            else:
                self.name = item_name[self.id]

        if self.category == "":
            if self.id in item_type:
                self.category = item_type[self.id]
            else:
                self.category = "Item"

    def conflictcheck(self, pts, pt):
        """
        pt が ptsのどれかと衝突していたら面積に応じて入れ替える
        """
        flag = False
        for p in list(pts):
            if has_intersect(p, pt):
                # どちらかを消す
                p_area = (p[2]-p[0])*(p[3]-p[1])
                pt_area = (pt[2]-pt[0])*(pt[3]-pt[1])
                if p_area < pt_area:
                    pts.remove(p)
                else:
                    flag = True

        if flag is False:
            pts.append(pt)
        return pts

    def extension(self, pts):
        """
        文字エリアを1pixcel微修正
        """
        new_pts = []
        for pt in pts:
            if pt[0] == 0 and pt[1] == 0:
                pt = [pt[0], pt[1], pt[2], pt[3] + 1]
            elif pt[0] == 0 and pt[1] != 0:
                pt = [pt[0], pt[1] - 1, pt[2], pt[3] + 1]
            elif pt[0] != 0 and pt[1] == 0:
                pt = [pt[0] - 1, pt[1], pt[2], pt[3] + 1]
            else:
                pt = [pt[0] - 1, pt[1] - 1, pt[2], pt[3] + 1]
            new_pts.append(pt)
        return new_pts

    def extension_straighten(self, pts):
        """
        Y軸を最大値にそろえつつ文字エリアを1pixcel微修正
        """
        base_top = 6  # 強制的に高さを確保
        base_bottom = 10
        for pt in pts:
            if base_top > pt[1]:
                base_top = pt[1]
            if base_bottom < pt[3]:
                base_bottom = pt[3]

        # 5桁目がおかしくなる対策
        new_pts = []
        pts.reverse()
        for i, pt in enumerate(pts):
            if len(pts) > 6 and i == 4:
                pt = [pts[5][2], base_top, pts[3][0], base_bottom]
            else:
                pt = [pt[0], base_top, pt[2], base_bottom]
            new_pts.append(pt)
        new_pts.reverse()
        return new_pts

    def detect_bonus_char4jpg(self, mode):
        """
        [JP]Ver.2.37.0以前の仕様
        戦利品数OCRで下段の黄文字の座標を抽出する
        PNGではない画像の認識用

        """
        # QP,ポイントはボーナス6桁のときに高さが変わる
        # それ以外は3桁のときに変わるはず(未確認)
        # ここのmargin_right はドロップ数の下一桁目までの距離
        base_line = 181 if mode == "na" else 179
        pattern_tiny = r"^\(\+\d{4,5}0\)$"
        pattern_small = r"^\(\+\d{5}0\)$"
        pattern_normal = r"^\(\+[1-9]\d*\)$"
        # 1-5桁の読み込み
        font_size = FONTSIZE_NORMAL
        if mode == 'na':
            margin_right = 20
        else:
            margin_right = 26
        line, pts = self.get_number4jpg(base_line, margin_right, font_size)
        logger.debug("Read BONUS NORMAL: %s", line)
        m_normal = re.match(pattern_normal, line)
        if m_normal:
            logger.debug("Font Size: %d", font_size)
            return line, pts, font_size
        # 6桁の読み込み
        if mode == 'na':
            margin_right = 19
        else:
            margin_right = 25
        font_size = FONTSIZE_SMALL
        line, pts = self.get_number4jpg(base_line, margin_right, font_size)
        logger.debug("Read BONUS SMALL: %s", line)
        m_small = re.match(pattern_small, line)
        if m_small:
            logger.debug("Font Size: %d", font_size)
            return line, pts, font_size
        # 7桁読み込み
        font_size = FONTSIZE_TINY
        if mode == 'na':
            margin_right = 18
        else:
            margin_right = 26
        line, pts = self.get_number4jpg(base_line, margin_right, font_size)
        logger.debug("Read BONUS TINY: %s", line)
        m_tiny = re.match(pattern_tiny, line)
        if m_tiny:
            logger.debug("Font Size: %d", font_size)
            return line, pts, font_size
        else:
            font_size = FONTSIZE_UNDEFINED
            logger.debug("Font Size: %d", font_size)
            line = ""
            pts = []

        return line, pts, font_size

    def detect_bonus_char4jpg2(self, mode):
        """
        [JP]Ver.2.37.0以降の仕様
        戦利品数OCRで下段の黄文字の座標を抽出する
        PNGではない画像の認識用

        """
        # QP,ポイントはボーナス6桁のときに高さが変わる
        # それ以外は3桁のときに変わるはず(未確認)
        # ここのmargin_right はドロップ数の下一桁目までの距離
        base_line = 181 if mode == "na" else 179
        pattern_tiny = r"^\(\+\d{4,5}0\)$"
        pattern_small = r"^\(\+\d{5}0\)$"
        pattern_normal = r"^\(\+[1-9]\d*\)$"
        font_size = FONTSIZE_NEWSTYLE
        if mode == 'na':
            margin_right = 20
        else:
            margin_right = 26
        # 1-5桁の読み込み
        cut_width = 21
        comma_width = 5
        line, pts = self.get_number4jpg2(base_line, margin_right, cut_width, comma_width)
        logger.debug("Read BONUS NORMAL: %s", line)
        m_normal = re.match(pattern_normal, line)
        if m_normal:
            logger.debug("Font Size: %d", font_size)
            return line, pts, font_size
        # 6桁の読み込み
        cut_width = 19
        comma_width = 5

        line, pts = self.get_number4jpg2(base_line, margin_right, cut_width, comma_width)
        logger.debug("Read BONUS SMALL: %s", line)
        m_small = re.match(pattern_small, line)
        if m_small:
            logger.debug("Font Size: %d", font_size)
            return line, pts, font_size
        # 7桁読み込み
        cut_width = 18
        comma_width = 5

        line, pts = self.get_number4jpg2(base_line, margin_right, cut_width, comma_width)
        logger.debug("Read BONUS TINY: %s", line)
        m_tiny = re.match(pattern_tiny, line)
        if m_tiny:
            logger.debug("Font Size: %d", font_size)
            return line, pts, font_size
        else:
            font_size = FONTSIZE_UNDEFINED
            logger.debug("Font Size: %d", font_size)
            line = ""
            pts = []

        return line, pts, font_size

    def detect_bonus_char(self):
        """
        戦利品数OCRで下段の黄文字の座標を抽出する

        HSVで黄色をマスクしてオブジェクト検出
        ノイズは少なく精度はかなり良い
        """

        margin_top = int(self.height*0.72)
        margin_bottom = int(self.height*0.11)
        margin_left = 8
        margin_right = 8

        img_hsv_lower = self.img_hsv[margin_top: self.height - margin_bottom,
                                     margin_left: self.width - margin_right]

        h, w = img_hsv_lower.shape[:2]
        # 手持ちスクショでうまくいっている範囲
        # 黄文字がこの数値でマスクできるかが肝
        # 未対応機種が発生したため[25,180,119] →[25,175,119]に変更
        lower_yellow = np.array([25, 175, 119])
        upper_yellow = np.array([37, 255, 255])

        img_hsv_lower_mask = cv2.inRange(img_hsv_lower,
                                         lower_yellow, upper_yellow)

        contours = cv2.findContours(img_hsv_lower_mask, cv2.RETR_TREE,
                                    cv2.CHAIN_APPROX_SIMPLE)[0]

        bonus_pts = []
        # 物体検出マスクがうまくいっているかが成功の全て
        for cnt in contours:
            ret = cv2.boundingRect(cnt)
            area = cv2.contourArea(cnt)
            pt = [ret[0] + margin_left, ret[1] + margin_top,
                  ret[0] + ret[2] + margin_left, ret[1] + ret[3] + margin_top]

            # ）が上下に割れることがあるので上の一つは消す
            if ret[2] < int(w/2) and ret[1] < int(h*3/5) \
               and ret[1] + ret[3] > h*0.65 and area > 3:
                bonus_pts = self.conflictcheck(bonus_pts, pt)

        bonus_pts.sort()
        if len(bonus_pts) > 0:
            if self.width - bonus_pts[-1][2] > int((22*self.width/188)):
                # 黄文字は必ず右寄せなので最後の文字が画面端から離れている場合全部ゴミ
                bonus_pts = []

        return self.extension(bonus_pts)

    def define_fontsize(self, font_size):
        if font_size == FONTSIZE_NORMAL:
            cut_width = 20
            cut_height = 28
            comma_width = 9
        elif font_size == FONTSIZE_SMALL:
            cut_width = 18
            cut_height = 25
            comma_width = 8
        else:
            cut_width = 16
            cut_height = 22
            comma_width = 6
        return cut_width, cut_height, comma_width

    def get_number4jpg(self, base_line, margin_right, font_size):
        """[JP]Ver.2.37.0以前の仕様
        """
        cut_width, cut_height, comma_width = self.define_fontsize(font_size)
        top_y = base_line - cut_height
        # まず、+, xの位置が何桁目か調査する
        pts = []
        if font_size == FONTSIZE_TINY:
            max_digits = 8
        elif font_size == FONTSIZE_SMALL:
            max_digits = 8
        else:
            max_digits = 7

        for i in range(max_digits):
            if i == 0:
                continue
            pt = [self.width - margin_right - cut_width * (i + 1)
                  - comma_width * int((i - 1)/3),
                  top_y,
                  self.width - margin_right - cut_width * i
                  - comma_width * int((i - 1)/3),
                  base_line]
            result = self.read_char(pt)
            if i == 1 and ord(result) == 0:
                # アイテム数 x1 とならず表記無し場合のエラー処理
                return "", pts
            if result in ['x', '+']:
                break
        # 決まった位置まで出力する
        line = ""
        for j in range(i):
            pt = [self.width - margin_right - cut_width * (j + 1)
                  - comma_width * int(j/3),
                  top_y,
                  self.width - margin_right - cut_width * j
                  - comma_width * int(j/3),
                  base_line]
            c = self.read_char(pt)
            if ord(c) == 0:  # Null文字対策
                line = line + '?'
                break
            line = line + c
            pts.append(pt)
        j = j + 1
        pt = [self.width - margin_right - cut_width * (j + 1)
              - comma_width * int((j - 1)/3),
              top_y,
              self.width - margin_right - cut_width * j
              - comma_width * int((j - 1)/3),
              base_line]
        c = self.read_char(pt)
        if ord(c) == 0:  # Null文字対策
            c = '?'
        line = line + c
        line = "(" + line[::-1] + ")"
        pts.append(pt)
        pts.sort()
        # PNGのマスク法との差を埋める補正
        new_pts = [[pts[0][0]-10, pts[0][1],
                    pts[0][0]-1, pts[0][3]]]  # "(" に対応
        new_pts.append("")  # ")" に対応

        return line, new_pts

    def get_number4jpg2(self, base_line, margin_right, cut_width, comma_width):
        """[JP]Ver.2.37.0以降の仕様
            
        """
        cut_height = 30
        top_y = base_line - cut_height
        # まず、+, xの位置が何桁目か調査する
        pts = []
        max_digits = 7

        for i in range(max_digits):
            if i == 0:
                continue
            pt = [self.width - margin_right - cut_width * (i + 1)
                  - comma_width * int((i - 1)/3),
                  top_y,
                  self.width - margin_right - cut_width * i
                  - comma_width * int((i - 1)/3),
                  base_line]
            result = self.read_char(pt)
            if i == 1 and ord(result) == 0:
                # アイテム数 x1 とならず表記無し場合のエラー処理
                return "", pts
            if result in ['x', '+']:
                break
        # 決まった位置まで出力する
        line = ""
        for j in range(i):
            pt = [self.width - margin_right - cut_width * (j + 1)
                  - comma_width * int(j/3),
                  top_y,
                  self.width - margin_right - cut_width * j
                  - comma_width * int(j/3),
                  base_line]
            c = self.read_char(pt)
            if ord(c) == 0:  # Null文字対策
                line = line + '?'
                break
            line = line + c
            pts.append(pt)
        j = j + 1
        pt = [self.width - margin_right - cut_width * (j + 1)
              - comma_width * int((j - 1)/3),
              top_y,
              self.width - margin_right - cut_width * j
              - comma_width * int((j - 1)/3),
              base_line]
        c = self.read_char(pt)
        if ord(c) == 0:  # Null文字対策
            c = '?'
        line = line + c
        line = "(" + line[::-1] + ")"
        pts.append(pt)
        pts.sort()
        # PNGのマスク法との差を埋める補正
        new_pts = [[pts[0][0]-10, pts[0][1],
                    pts[0][0]-1, pts[0][3]]]  # "(" に対応
        new_pts.append("")  # ")" に対応

        return line, new_pts

    def get_number(self, base_line, margin_right, font_size):
        """[JP]Ver.2.37.0以前の仕様
        """
        cut_width, cut_height, comma_width = self.define_fontsize(font_size)
        top_y = base_line - cut_height
        # まず、+, xの位置が何桁目か調査する
        for i in range(8):  # 8桁以上は無い
            if i == 0:
                continue
            elif (self.id == ID_REWARD_QP
                  or self.category in ["Point"]) and i <= 2:
                # 報酬QPとPointは3桁以上
                continue
            elif self.name == "QP" and i <= 3:
                # QPは4桁以上
                continue
            pt = [self.width - margin_right - cut_width * (i + 1)
                  - comma_width * int((i - 1)/3),
                  top_y,
                  self.width - margin_right - cut_width * i
                  - comma_width * int((i - 1)/3),
                  base_line]
            if pt[0] < 0:
                break
            result = self.read_char(pt)
            if i == 1 and ord(result) == 0:
                # アイテム数 x1 とならず表記無し場合のエラー処理
                return ""
            if result in ['x', '+']:
                self.margin_left = pt[0]
                break
        # 決まった位置まで出力する
        line = ""
        for j in range(i):
            if (self.id == ID_REWARD_QP) and j < 1:
                # 報酬QPの下一桁は0
                line += '0'
                continue
            elif (self.name == "QP" or self.category in ["Point"]) and j < 2:
                # QPとPointは下二桁は00
                line += '0'
                continue
            pt = [self.width - margin_right - cut_width * (j + 1)
                  - comma_width * int(j/3),
                  top_y,
                  self.width - margin_right - cut_width * j
                  - comma_width * int(j/3),
                  base_line]
            if pt[0] < 0:
                break
            c = self.read_char(pt)
            if ord(c) == 0:  # Null文字対策
                c = '?'
            line = line + c
        j = j + 1
        pt = [self.width - margin_right - cut_width * (j + 1)
              - comma_width * int((j - 1)/3),
              top_y,
              self.width - margin_right - cut_width * j
              - comma_width * int((j - 1)/3),
              base_line]
        if pt[0] > 0:
            c = self.read_char(pt)
            if ord(c) == 0:  # Null文字対策
                c = '?'
            line = line + c
        line = line[::-1]

        return line

    def get_number2(self, cut_width, comma_width):
        """[JP]Ver.2.37.0以降の仕様
        """
        cut_height = 26
        base_line = 147
        margin_right = 15
        top_y = base_line - cut_height
        # まず、+, xの位置が何桁目か調査する
        for i in range(8):  # 8桁以上は無い
            if i == 0:
                continue
            elif (self.id == ID_REWARD_QP
                  or self.category in ["Point"]) and i <= 2:
                # 報酬QPとPointは3桁以上
                continue
            elif self.name == "QP" and i <= 3:
                # QPは4桁以上
                continue
            pt = [self.width - margin_right - cut_width * (i + 1)
                  - comma_width * int((i - 1)/3),
                  top_y,
                  self.width - margin_right - cut_width * i
                  - comma_width * int((i - 1)/3),
                  base_line]
            if pt[0] < 0:
                break
            result = self.read_char(pt)
            if i == 1 and ord(result) == 0:
                # アイテム数 x1 とならず表記無し場合のエラー処理
                return ""
            if result in ['x', '+']:
                self.margin_left = pt[0]
                break
        # 決まった位置まで出力する
        line = ""
        for j in range(i):
            if (self.id == ID_REWARD_QP) and j < 1:
                # 報酬QPの下一桁は0
                line += '0'
                continue
            elif (self.name == "QP" or self.category in ["Point"]) and j < 2:
                # QPとPointは下二桁は00
                line += '0'
                continue
            pt = [self.width - margin_right - cut_width * (j + 1)
                  - comma_width * int(j/3),
                  top_y,
                  self.width - margin_right - cut_width * j
                  - comma_width * int(j/3),
                  base_line]
            if pt[0] < 0:
                break
            c = self.read_char(pt)
            if ord(c) == 0:  # Null文字対策
                c = '?'
            line = line + c
        j = j + 1
        pt = [self.width - margin_right - cut_width * (j + 1)
              - comma_width * int((j - 1)/3),
              top_y,
              self.width - margin_right - cut_width * j
              - comma_width * int((j - 1)/3),
              base_line]
        if pt[0] > 0:
            c = self.read_char(pt)
            if ord(c) == 0:  # Null文字対策
                c = '?'
            line = line + c
        line = line[::-1]

        return line


    def detect_white_char(self, base_line, margin_right, mode="jp"):
        """
        上段と下段の白文字を見つける機能を一つに統合
        [JP]Ver.2.37.0からボーナスがある場合の表示の仕様変更有り
        """
        pattern_tiny = r"^[\+x][12]\d{4}00$"
        pattern_tiny_qp = r"^\+[12]\d{4,5}00$"
        pattern_small = r"^[\+x]\d{4}00$"
        pattern_small_qp = r"^\+\d{4,5}00$"
        pattern_normal = r"^[\+x][1-9]\d{0,5}$"
        pattern_normal_qp = r"^\+[1-9]\d{0,4}0$"
        logger.debug("base_line: %d", base_line)
        if mode=="jp" and base_line < 170:
            # JP Ver.2.37.0以降の新仕様
            # 1-6桁の読み込み
            font_size = FONTSIZE_NEWSTYLE
            cut_width = 21
            comma_width = 5
            line = self.get_number2(cut_width, comma_width)
            logger.debug("Read NORMAL: %s", line)
            if self.id == ID_QP or self.category == "Point":
                pattern_normal = pattern_normal_qp
            m_normal = re.match(pattern_normal, line)
            if m_normal:
                logger.debug("Font Size: %d", font_size)
                self.font_size = font_size
                return line
            # 6桁の読み込み
            cut_width = 19
            comma_width = 5
            line = self.get_number2(cut_width, comma_width)
            logger.debug("Read SMALL: %s", line)
            if self.id == ID_QP or self.category == "Point":
                pattern_small = pattern_small_qp
            m_small = re.match(pattern_small, line)
            if m_small:
                logger.debug("Font Size: %d", font_size)
                self.font_size = font_size
                return line
            # 7桁読み込み
            cut_width = 19
            comma_width = 4
            line = self.get_number2(cut_width, comma_width)
            logger.debug("Read TINY: %s", line)
            if self.id == ID_QP or self.category == "Point":
                pattern_tiny = pattern_tiny_qp
            m_tiny = re.match(pattern_tiny, line)
            if m_tiny:
                logger.debug("Font Size: %d", font_size)
                self.font_size = font_size
                return line
        else:
            # JP Ver.2.37.0以前の旧仕様
            if self.font_size != FONTSIZE_UNDEFINED:
                line = self.get_number(base_line, margin_right, self.font_size)
                logger.debug("line: %s", line)
                if len(line) <= 1:
                    return ""
                elif not line[1:].isdigit():
                    return ""
                return line
            else:
                # 1-6桁の読み込み
                font_size = FONTSIZE_NORMAL
                line = self.get_number(base_line, margin_right, font_size)
                logger.debug("Read NORMAL: %s", line)
                if self.id == ID_QP or self.category == "Point":
                    pattern_normal = pattern_normal_qp
                m_normal = re.match(pattern_normal, line)
                if m_normal:
                    logger.debug("Font Size: %d", font_size)
                    self.font_size = font_size
                    return line
                # 6桁の読み込み
                font_size = FONTSIZE_SMALL
                line = self.get_number(base_line, margin_right, font_size)
                logger.debug("Read SMALL: %s", line)
                if self.id == ID_QP or self.category == "Point":
                    pattern_small = pattern_small_qp
                m_small = re.match(pattern_small, line)
                if m_small:
                    logger.debug("Font Size: %d", font_size)
                    self.font_size = font_size
                    return line
                # 7桁読み込み
                font_size = FONTSIZE_TINY
                line = self.get_number(base_line, margin_right, font_size)
                logger.debug("Read TINY: %s", line)
                if self.id == ID_QP or self.category == "Point":
                    pattern_tiny = pattern_tiny_qp
                m_tiny = re.match(pattern_tiny, line)
                if m_tiny:
                    logger.debug("Font Size: %d", font_size)
                    self.font_size = font_size
                    return line
        return ""

    def read_item(self, pts):
        """
        ボーナスの数値をOCRする(エラー訂正有)
        """
        win_size = (120, 60)
        block_size = (16, 16)
        block_stride = (4, 4)
        cell_size = (4, 4)
        bins = 9
        lines = ""

        for pt in pts:
            char = []
            tmpimg = self.img_gray[pt[1]:pt[3], pt[0]:pt[2]]
            tmpimg = cv2.resize(tmpimg, (win_size))
            hog = cv2.HOGDescriptor(win_size, block_size, block_stride,
                                    cell_size, bins)
            char.append(hog.compute(tmpimg))
            char = np.array(char)
            pred = self.svm.predict(char)
            result = int(pred[1][0][0])
            if result != 0:
                lines = lines + chr(result)
        logger.debug("OCR Result: %s", lines)
        # 以下エラー訂正
        if not lines.endswith(")"):
            lines = lines[:-1] + ")"
        if not lines.startswith("(+") and not lines.startswith("(x"):
            if lines[0] in ["+", 'x']:
                lines = "(" + lines
            else:
                lines = ""
        lines = lines.replace("()", "0")
        if len(lines) > 1:
            # エラー訂正 文字列左側
            # 主にイベントのポイントドロップで左側にゴミができるが、
            # 特定の記号がでてきたらそれより前はデータが無いはずなので削除する
            point_lbra = lines.rfind("(")
            point_plus = lines.rfind("+")
            point_x = lines.rfind("x")
            if point_lbra != -1:
                lines = lines[point_lbra:]
            elif point_plus != -1:
                lines = lines[point_plus:]
            elif point_x != -1:
                lines = lines[point_x:]

        if lines.isdigit():
            if int(lines) == 0:
                lines = "xErr"
            elif self.name == "QP" or self.name == "クエストクリア報酬QP":
                lines = '+' + lines
            else:
                if int(lines) >= 100:
                    lines = '+' + lines
                else:
                    lines = 'x' + lines

        if len(lines) == 1:
            lines = "xErr"

        return lines

    def read_char(self, pt):
        """
        戦利品の数値1文字をOCRする
        白文字検出で使用
        """
        win_size = (120, 60)
        block_size = (16, 16)
        block_stride = (4, 4)
        cell_size = (4, 4)
        bins = 9
        char = []
        tmpimg = self.img_gray[pt[1]:pt[3], pt[0]:pt[2]]
        tmpimg = cv2.resize(tmpimg, (win_size))
        hog = cv2.HOGDescriptor(win_size, block_size, block_stride,
                                cell_size, bins)
        char.append(hog.compute(tmpimg))
        char = np.array(char)
        pred = self.svm.predict(char)
        result = int(pred[1][0][0])
        return chr(result)

    def ocr_digit(self, mode='jp'):
        """
        戦利品OCR
        """
        self.font_size = FONTSIZE_UNDEFINED

        if self.prev_item is None:
            prev_id = -1
        else:
            prev_id = self.prev_item.id

        logger.debug("self.id: %d", self.id)
        logger.debug("prev_id: %d", prev_id)
        if prev_id == self.id:
            self.dropnum_cache = self.prev_item.dropnum_cache
        if prev_id == self.id \
                and not (ID_GEM_MAX <= self.id <= ID_MONUMENT_MAX):
            # もしキャッシュ画像と一致したらOCRスキップ
            logger.debug("dropnum_cache: %s", self.prev_item.dropnum_cache)
            for dropnum_cache in self.prev_item.dropnum_cache:
                pts = dropnum_cache["pts"]
                img_gray = self.img_gray[pts[0][1]-2:pts[1][1]+2,
                                         pts[0][0]-2:pts[1][0]+2]
                template = dropnum_cache["img"]
                res = cv2.matchTemplate(img_gray, template,
                                        cv2.TM_CCOEFF_NORMED)
                threshold = 0.97
                loc = np.where(res >= threshold)
                find_match = False
                for pt in zip(*loc[::-1]):
                    find_match = True
                    break
                if find_match:
                    logger.debug("find_match")
                    self.bonus = dropnum_cache["bonus"]
                    self.dropnum = dropnum_cache["dropnum"]
                    self.bonus_pts = dropnum_cache["bonus_pts"]
                    return
            logger.debug("not find_match")

        if ID_GEM_MAX <= self.id <= ID_MONUMENT_MAX:
            # ボーナスが無いアイテム
            self.bonus_pts = []
            self.bonus = ""
            self.font_size = FONTSIZE_NORMAL
        elif prev_id == self.id \
                and self.category != "Point" and self.name != "QP":
            self.bonus_pts = self.prev_item.bonus_pts
            self.bonus = self.prev_item.bonus
            self.font_size = self.prev_item.font_size
        elif self.fileextention.lower() == '.png':
            self.bonus_pts = self.detect_bonus_char()
            self.bonus = self.read_item(self.bonus_pts)
            # フォントサイズを決定
            if len(self.bonus_pts) > 0:
                y_height = self.bonus_pts[-1][3] - self.bonus_pts[-1][1]
                logger.debug("y_height: %s", y_height)
                if self.position >= 14:
                    self.font_size = FONTSIZE_UNDEFINED
                elif y_height < 25:
                    self.font_size = FONTSIZE_TINY
                elif y_height > 27:
                    self.font_size = FONTSIZE_NORMAL
                else:
                    self.font_size = FONTSIZE_SMALL
        else:
            if mode == "jp":
                self.bonus, self.bonus_pts, self.font_size = self.detect_bonus_char4jpg2(mode)
            else:
                self.bonus, self.bonus_pts, self.font_size = self.detect_bonus_char4jpg(mode)
        logger.debug("Bonus Font Size: %s", self.font_size)

        # 実際の(ボーナス無し)ドロップ数が上段にあるか下段にあるか決定
        offsset_y = 2 if mode == 'na' else 0
        if (self.category in ["Quest Reward", "Point"] or self.name == "QP") \
           and len(self.bonus) >= 5:  # ボーナスは"(+*0)"なので
            # 1桁目の上部からの距離を設定
            base_line = self.bonus_pts[-2][1] - 3 + offsset_y
        else:
            base_line = int(180/206*self.height)

        # 実際の(ボーナス無し)ドロップ数の右端の位置を決定
        offset_x = -7 if mode == "na" else 0
        if self.category in ["Quest Reward", "Point"] or self.name == "QP":
            margin_right = 15 + offset_x
        elif len(self.bonus_pts) > 0:
            margin_right = self.width - self.bonus_pts[0][0] + 2
        else:
            margin_right = 15 + offset_x
        logger.debug("margin_right: %d", margin_right)
        self.dropnum = self.detect_white_char(base_line, margin_right, mode)
        logger.debug("self.dropnum: %s", self.dropnum)
        if len(self.dropnum) == 0:
            self.dropnum = "x1"
        if self.id != ID_REWARD_QP \
                and not (ID_GEM_MAX <= self.id <= ID_MONUMENT_MAX):
            dropnum_found = False
            for cache_item in self.dropnum_cache:
                if cache_item["dropnum"] == self.dropnum:
                    dropnum_found = True
                    break
            if dropnum_found is False:
                # キャッシュのために画像を取得する
                _, width = self.img_gray.shape
                _, cut_height, _ = self.define_fontsize(self.font_size)
                logger.debug("base_line: %d", base_line)
                logger.debug("cut_height: %d", cut_height)
                logger.debug("margin_right: %d", margin_right)
                pts = ((self.margin_left, base_line - cut_height),
                       (width - margin_right, base_line))
                cached_img = self.img_gray[pts[0][1]:pts[1][1],
                                           pts[0][0]:pts[1][0]]
                tmp = {}
                tmp["dropnum"] = self.dropnum
                tmp["img"] = cached_img
                tmp["pts"] = pts
                tmp["bonus"] = self.bonus
                tmp["bonus_pts"] = self.bonus_pts
                self.dropnum_cache.append(tmp)

    def gem_img2id(self, img, gem_dict):
        hash_gem = self.compute_gem_hash(img)
        gems = {}
        for i in gem_dict.keys():
            d2 = hasher.compare(hash_gem, hex2hash(gem_dict[i]))
            if d2 <= 20:
                gems[i] = d2
        gems = sorted(gems.items(), key=lambda x: x[1])
        gem = next(iter(gems))
        return gem[0]

    def classify_item(self, img, currnet_dropPriority):

        """)
        imgとの距離を比較して近いアイテムを求める
        id を返すように変更
        """
        hash_item = self.hash_item  # 画像の距離
        ids = {}
        if logger.isEnabledFor(logging.DEBUG):
            hex = ""
            for h in hash_item[0]:
                hex = hex + "{:02x}".format(h)
            logger.debug("phash: %s", hex)
        # 既存のアイテムとの距離を比較
        for i in dist_item.keys():
            itemid = dist_item[i]
            item_bg = item_background[itemid]
            d = hasher.compare(hash_item, hex2hash(i))
            if d <= 12 and item_bg == self.background:
                # ポイントと種の距離が8という例有り(IMG_0274)→16に
                # バーガーと脂の距離が10という例有り(IMG_2354)→14に
                ids[dist_item[i]] = d
        if len(ids) > 0:
            ids = sorted(ids.items(), key=lambda x: x[1])
            id_tupple = next(iter(ids))
            id = id_tupple[0]
            if ID_SECRET_GEM_MIN <= id <= ID_SECRET_GEM_MAX:
                if currnet_dropPriority >= PRIORITY_SECRET_GEM_MIN:
                    id = self.gem_img2id(img, dist_secret_gem)
                else:
                    return ""
            elif ID_MAGIC_GEM_MIN <= id <= ID_MAGIC_GEM_MAX:
                if currnet_dropPriority >= PRIORITY_MAGIC_GEM_MIN:
                    id = self.gem_img2id(img, dist_magic_gem)
                else:
                    return ""
            elif ID_GEM_MIN <= id <= ID_GEM_MAX:
                if currnet_dropPriority >= PRIORITY_GEM_MIN:
                    id = self.gem_img2id(img, dist_gem)
                else:
                    return ""

            return id

        return ""

    def classify_ce_sub(self, img, hasher_prog, dist_dic, threshold):
        """
        imgとの距離を比較して近いアイテムを求める
        """
        hash_item = hasher_prog(img)  # 画像の距離
        itemfiles = {}
        if logger.isEnabledFor(logging.DEBUG):
            hex = ""
            for h in hash_item[0]:
                hex = hex + "{:02x}".format(h)
        # 既存のアイテムとの距離を比較
        for i in dist_dic.keys():
            d = hasher.compare(hash_item, hex2hash(i))
            if d <= threshold:
                itemfiles[dist_dic[i]] = d
        if len(itemfiles) > 0:
            itemfiles = sorted(itemfiles.items(), key=lambda x: x[1])
            logger.debug("itemfiles: %s", itemfiles)
            item = next(iter(itemfiles))

            return item[0]

        return ""

    def classify_ce(self, img):
        itemid = self.classify_ce_sub(img, compute_hash_ce, dist_ce, 12)
        if itemid == "":
            logger.debug("use narrow image")
            itemid = self.classify_ce_sub(
                        img, compute_hash_ce_narrow, dist_ce_narrow, 15
                        )
        return itemid

    def classify_point(self, img):
        """
        imgとの距離を比較して近いアイテムを求める
        """
        hash_item = compute_hash(img)  # 画像の距離
        itemfiles = {}
        if logger.isEnabledFor(logging.DEBUG):
            hex = ""
            for h in hash_item[0]:
                hex = hex + "{:02x}".format(h)
            logger.debug("phash: %s", hex)
        # 既存のアイテムとの距離を比較
        for i in dist_point.keys():
            itemid = dist_point[i]
            item_bg = item_background[itemid]
            d = hasher.compare(hash_item, hex2hash(i))
            if d <= 12 and item_bg == self.background:
                itemfiles[itemid] = d
        if len(itemfiles) > 0:
            itemfiles = sorted(itemfiles.items(), key=lambda x: x[1])
            item = next(iter(itemfiles))

            return item[0]

        return ""

    def classify_exp(self, img):
        hash_item = self.compute_exp_rarity_hash(img)  # 画像の距離
        exps = {}
        for i in dist_exp_rarity.keys():
            dt = hasher.compare(hash_item, hex2hash(i))
            if dt <= 15:  # IMG_1833で11 IMG_1837で15
                exps[i] = dt
        exps = sorted(exps.items(), key=lambda x: x[1])
        if len(exps) > 0:
            exp = next(iter(exps))

            hash_exp_class = self.compute_exp_class_hash(img)
            exp_classes = {}
            for j in dist_exp_class.keys():
                dtc = hasher.compare(hash_exp_class, hex2hash(j))
                exp_classes[j] = dtc
            exp_classes = sorted(exp_classes.items(), key=lambda x: x[1])
            exp_class = next(iter(exp_classes))

            return int(str(dist_exp_class[exp_class[0]])[:4]
                       + str(dist_exp_rarity[exp[0]])[4] + "00")

        return ""

    def make_new_file(self, img, search_dir, dist_dic, dropPriority, category):
        """
        ファイル名候補を探す
        """
        i_dic = {"Item": "item", "Craft Essence": "ce", "Point": "point"}
        initial = i_dic[category]
        for i in range(999):
            itemfile = search_dir / (initial + '{:0=3}'.format(i + 1) + '.png')
            if itemfile.is_file():
                continue
            else:
                cv2.imwrite(itemfile.as_posix(), img)
                # id 候補を決める
                for j in range(99999):
                    id = j + ID_START
                    if id in item_name.keys():
                        continue
                    break
                if category == "Craft Essence":
                    hash = compute_hash_ce(img)
                else:
                    hash = compute_hash(img)
                hash_hex = ""
                for h in hash[0]:
                    hash_hex = hash_hex + "{:02x}".format(h)
                dist_dic[hash_hex] = id
                if category == "Craft Essence":
                    hash_narrow = compute_hash_ce_narrow(img)
                    hash_hex_narrow = ""
                    for h in hash_narrow[0]:
                        hash_narrow = hash_narrow + "{:02x}".format(h)
                    dist_ce_narrow[hash_hex_narrow] = id
                item_name[id] = itemfile.stem
                item_background[id] = classify_background(img)
                item_dropPriority[id] = dropPriority
                item_type[id] = category
                break
        return id

    def classify_category(self, svm_card):
        """
        カード判別器
       """
        """
        カード判別器
        この場合は画像全域のハッシュをとる
        """
        # Hog特徴のパラメータ
        win_size = (120, 60)
        block_size = (16, 16)
        block_stride = (4, 4)
        cell_size = (4, 4)
        bins = 9
        test = []
        carddic = {0: 'Quest Reward', 1: 'Item', 2: 'Point',
                   3: 'Craft Essence', 4: 'Exp. UP', 99: ""}

        tmpimg = self.img_rgb[int(189/206*self.height):
                              int(201/206*self.height),
                              int(78/188*self.width):
                              int(115/188*self.width)]

        tmpimg = cv2.resize(tmpimg, (win_size))
        hog = cv2.HOGDescriptor(win_size, block_size, block_stride,
                                cell_size, bins)
        test.append(hog.compute(tmpimg))  # 特徴量の格納
        test = np.array(test)
        pred = svm_card.predict(test)

        return carddic[pred[1][0][0]]

    def classify_card(self, img, currnet_dropPriority):
        """
        アイテム判別器
        """
        if self.category == "Point":
            id = self.classify_point(img)
            if id == "":
                id = self.make_new_file(img, Point_dir, dist_point,
                                        PRIORITY_POINT, self.category)
            return id
        elif self.category == "Quest Reward":
            return 5
        elif self.category == "Craft Essence":
            id = self.classify_ce(img)
            if id == "":
                id = self.make_new_file(img, CE_dir, dist_ce,
                                        PRIORITY_CE, self.category)
            return id
        elif self.category == "Exp. UP":
            return self.classify_exp(img)
        elif self.category == "Item":
            id = self.classify_item(img, currnet_dropPriority)
            if id == "":
                id = self.make_new_file(img, Item_dir, dist_item,
                                        PRIORITY_ITEM, self.category)
        else:
            # ここで category が判別できないのは三行目かつ
            # スクロール位置の関係で下部表示が消えている場合
            id = self.classify_item(img, currnet_dropPriority)
            if id != "":
                return id
            id = self.classify_point(img)
            if id != "":
                return id
            id = self.classify_ce(img)
            if id != "":
                return id
            id = self.classify_exp(img)
            if id != "":
                return id
        if id == "":
            id = self.make_new_file(img, Item_dir, dist_item,
                                    PRIORITY_ITEM, "Item")
        return id

    def compute_exp_rarity_hash(self, img_rgb):
        """
        種火レアリティ判別器
        この場合は画像全域のハッシュをとる
        """
        img = img_rgb[int(53/189*self.height):int(136/189*self.height),
                      int(37/206*self.width):int(149/206*self.width)]

        return hasher.compute(img)

    def compute_exp_class_hash(self, img_rgb):
        """
        種火クラス判別器
        左上のクラスマークぎりぎりのハッシュを取る
        記述した比率はiPhone6S画像の実測値
        """
        img = img_rgb[int(5/135*self.height):int(30/135*self.height),
                      int(5/135*self.width):int(30/135*self.width)]
        return hasher.compute(img)

    def compute_gem_hash(self, img_rgb):
        """
        スキル石クラス判別器
        中央のクラスマークぎりぎりのハッシュを取る
        記述した比率はiPhone6S画像の実測値
        """
        height, width = img_rgb.shape[:2]

        img = img_rgb[int((145-16-60*0.8)/2/145*height)+3:
                      int((145-16+60*0.8)/2/145*height)+3,
                      int((132-52*0.8)/2/132*width):
                      int((132+52*0.8)/2/132*width)]

        return hasher.compute(img)


def classify_background(img_rgb):
    """
    背景判別
    """
    img = img_rgb[30:119, 7:25]
    target_hist = img_hist(img)
    bg_score = []
    score_z = calc_hist_score(target_hist, hist_zero)
    bg_score.append({"background": "zero", "dist": score_z})
    score_g = calc_hist_score(target_hist, hist_gold)
    bg_score.append({"background": "gold", "dist": score_g})
    score_s = calc_hist_score(target_hist, hist_silver)
    bg_score.append({"background": "silver", "dist": score_s})
    score_b = calc_hist_score(target_hist, hist_bronze)
    bg_score.append({"background": "bronze", "dist": score_b})

    bg_score = sorted(bg_score, key=lambda x: x['dist'])
    # logger.debug("background dist: %s", bg_score)
    return (bg_score[0]["background"])


def compute_hash(img_rgb):
    """
    判別器
    この判別器は下部のドロップ数を除いた部分を比較するもの
    記述した比率はiPhone6S画像の実測値
    """
    height, width = img_rgb.shape[:2]
    img = img_rgb[int(22/135*height):
                  int(77/135*height),
                  int(23/135*width):
                  int(112/135*width)]
    return hasher.compute(img)


def compute_hash_ce(img_rgb):
    """
    判別器
    この判別器は下部のドロップ数を除いた部分を比較するもの
    記述した比率はiPpd2018画像の実測値
    """
    img = img_rgb[12:176, 9:182]
    return hasher.compute(img)


def compute_hash_ce_narrow(img_rgb):
    """
    CE Identifier for scrolled down screenshot
    """
    height, width = img_rgb.shape[:2]
    img = img_rgb[int(30/206*height):int(155/206*height),
                  int(5/188*width):int(183/188*width)]
    return hasher.compute(img)


def search_file(search_dir, dist_dic, dropPriority, category):
    """
    Item, Craft Essence, Pointの各ファイルを探す
    """
    files = search_dir.glob('**/*.png')
    for fname in files:
        img = imread(fname)
        # id 候補を決める
        # 既存のデータがあったらそれを使用
        if fname.stem in item_name.values():
            id = [k for k, v in item_name.items() if v == fname.stem][0]
        elif fname.stem in item_shortname.values():
            id = [k for k, v in item_shortname.items() if v == fname.stem][0]
        else:
            for j in range(99999):
                id = j + ID_START
                if id in item_name.keys():
                    continue
                break
        # priotiry は固定
            item_name[id] = fname.stem
            item_dropPriority[id] = dropPriority
            item_type[id] = category
        if category == "Craft Essence":
            hash = compute_hash_ce(img)
        else:
            hash = compute_hash(img)
        hash_hex = ""
        for h in hash[0]:
            hash_hex = hash_hex + "{:02x}".format(h)
        dist_dic[hash_hex] = id
        if category == "Item" or category == "Point":
            item_background[id] = classify_background(img)
        if category == "Craft Essence":
            hash_narrow = compute_hash_ce_narrow(img)
            hash_hex_narrow = ""
            for h in hash_narrow[0]:
                hash_hex_narrow = hash_hex_narrow + "{:02x}".format(h)
            dist_ce_narrow[hash_hex_narrow] = id


def calc_hist_score(hist1, hist2):
    scores = []
    for channel1, channel2 in zip(hist1, hist2):
        score = cv2.compareHist(channel1, channel2, cv2.HISTCMP_BHATTACHARYYA)
        scores.append(score)
    return np.mean(scores)


def img_hist(img):
    hist1 = cv2.calcHist([img], [0], None, [256], [0, 256])
    hist2 = cv2.calcHist([img], [1], None, [256], [0, 256])
    hist3 = cv2.calcHist([img], [2], None, [256], [0, 256])

    return hist1, hist2, hist3


def calc_dist_local():
    """
    既所持のアイテム画像の距離(一次元配列)の辞書を作成して保持
    """
    search_file(Item_dir, dist_item, PRIORITY_ITEM, "Item")
    search_file(CE_dir, dist_ce, PRIORITY_CE, "Craft Essence")
    search_file(Point_dir, dist_point, PRIORITY_POINT, "Point")


def hex2hash(hexstr):
    hashlist = []
    for i in range(8):
        hashlist.append(int('0x' + hexstr[i*2:i*2+2], 0))
    return np.array([hashlist], dtype='uint8')


def out_name(args, id):
    if args.lang == "eng":
        if id in item_name_eng.keys():
            return item_name_eng[id]
    if id in item_shortname.keys():
        name = item_shortname[id]
    else:
        name = item_name[id]
    return name


def imread(filename, flags=cv2.IMREAD_COLOR, dtype=np.uint8):
    """
    OpenCVのimreadが日本語ファイル名が読めない対策
    """
    try:
        n = np.fromfile(filename, dtype)
        img = cv2.imdecode(n, flags)
        return img
    except Exception as e:
        logger.exception(e)
        return None


def get_exif(img):
    exif = img._getexif()
    try:
        for id, val in exif.items():
            tg = TAGS.get(id, id)
            if tg == "DateTimeOriginal":
                return datetime.datetime.strptime(val, '%Y:%m:%d %H:%M:%S')
    except AttributeError:
        return "NON"
    return "NON"


def get_output(filenames, args):
    """
    出力内容を作成
    """
    calc_dist_local()
    if train_item.exists() is False:
        logger.critical("item.xml is not found")
        logger.critical("Try to run 'python makeitem.py'")
        sys.exit(1)
    if train_chest.exists() is False:
        logger.critical("chest.xml is not found")
        logger.critical("Try to run 'python makechest.py'")
        sys.exit(1)
    if train_dcnt.exists() is False:
        logger.critical("dcnt.xml is not found")
        logger.critical("Try to run 'python makedcnt.py'")
        sys.exit(1)
    if train_card.exists() is False:
        logger.critical("card.xml is not found")
        logger.critical("Try to run 'python makecard.py'")
        sys.exit(1)
    svm = cv2.ml.SVM_load(str(train_item))
    svm_chest = cv2.ml.SVM_load(str(train_chest))
    svm_dcnt = cv2.ml.SVM_load(str(train_dcnt))
    svm_card = cv2.ml.SVM_load(str(train_card))

    fileoutput = []  # 出力
    prev_pages = 0
    prev_pagenum = 0
    prev_total_qp = QP_UNKNOWN
    prev_itemlist = []
    prev_datetime = datetime.datetime(year=2015, month=7, day=30, hour=0)
    prev_qp_gained = 0
    prev_chestnum = 0
    all_list = []

    for filename in filenames:
        logger.debug("filename: %s", filename)
        f = Path(filename)

        if f.exists() is False:
            output = {'filename': str(filename) + ': not found'}
            all_list.append([])
        elif f.is_dir():  # for ZIP file from MacOS
            pass
        elif f.suffix.upper() not in ['.PNG', '.JPG', '.JPEG']:
            output = {'filename': str(filename) + ': Not Supported'}
            all_list.append([])
        else:
            img_rgb = imread(filename)
            fileextention = Path(filename).suffix

            try:
                sc = ScreenShot(args, img_rgb,
                                svm, svm_chest, svm_dcnt, svm_card,
                                fileextention)
                if sc.itemlist[0]["id"] != ID_REWARD_QP and sc.pagenum == 1:
                    logger.warning(
                                   "Page count recognition is failing: %s",
                                   filename
                                   )
                # ドロップ内容が同じで下記のとき、重複除外
                # QPカンストじゃない時、QPが前と一緒
                # QPカンストの時、Exif内のファイル作成時間が15秒未満
                pilimg = Image.open(filename)
                dt = get_exif(pilimg)
                if dt == "NON" or prev_datetime == "NON":
                    td = datetime.timedelta(days=1)
                else:
                    td = dt - prev_datetime
                if sc.pages - sc.pagenum == 0:
                    sc.itemlist = sc.itemlist[14-(sc.lines+2) % 3*7:]
                if prev_itemlist == sc.itemlist:
                    if (sc.total_qp != -1 and sc.total_qp != 999999999
                        and sc.total_qp == prev_total_qp) \
                        or ((sc.total_qp == -1 or sc.total_qp == 999999999)
                            and td.total_seconds() < args.timeout):
                        logger.debug("args.timeout: %s", args.timeout)
                        logger.debug("filename: %s", filename)
                        logger.debug("prev_itemlist: %s", prev_itemlist)
                        logger.debug("sc.itemlist: %s", sc.itemlist)
                        logger.debug("sc.total_qp: %s", sc.total_qp)
                        logger.debug("prev_total_qp: %s", prev_total_qp)
                        logger.debug("datetime: %s", dt)
                        logger.debug("prev_datetime: %s", prev_datetime)
                        logger.debug("td.total_second: %s", td.total_seconds())
                        fileoutput.append(
                            {'filename': str(filename) + ': duplicate'})
                        all_list.append([])
                        continue

                # 2頁目以前のスクショが無い場合に migging と出力
                # 1. 前頁が最終頁じゃない&前頁の続き頁数じゃない
                # または前頁が最終頁なのに1頁じゃない
                # 2. 前頁の続き頁なのに獲得QPが違う
                if (
                    prev_pages - prev_pagenum > 0
                    and sc.pagenum - prev_pagenum != 1) \
                    or (prev_pages - prev_pagenum == 0
                        and sc.pagenum != 1) \
                    or sc.pagenum != 1 \
                        and sc.pagenum - prev_pagenum == 1 \
                        and (
                                prev_qp_gained != sc.qp_gained
                            ):
                    logger.debug("prev_pages: %s", prev_pages)
                    logger.debug("prev_pagenum: %s", prev_pagenum)
                    logger.debug("sc.pagenum: %s", sc.pagenum)
                    logger.debug("prev_qp_gained: %s", prev_qp_gained)
                    logger.debug("sc.qp_gained: %s", sc.qp_gained)
                    logger.debug("prev_chestnum: %s", prev_chestnum)
                    logger.debug("sc.chestnum: %s", sc.chestnum)
                    fileoutput.append({'filename': 'missing'})
                    all_list.append([])

                all_list.append(sc.itemlist)

                prev_pages = sc.pages
                prev_pagenum = sc.pagenum
                prev_total_qp = sc.total_qp
                prev_itemlist = sc.itemlist
                prev_datetime = dt
                prev_qp_gained = sc.qp_gained
                prev_chestnum = sc.chestnum

                sumdrop = len([d for d in sc.itemlist
                               if d["id"] != ID_REWARD_QP])
                if args.lang == "jpn":
                    drop_count = "ドロ数"
                else:
                    drop_count = "drop_count"
                output = {'filename': str(filename), drop_count: sumdrop}
                if sc.pagenum == 1:
                    if sc.lines >= 7:
                        output[drop_count] = str(output[drop_count]) + "++"
                    elif sc.lines >= 4:
                        output[drop_count] = str(output[drop_count]) + "+"
                elif sc.pagenum == 2 and sc.lines >= 7:
                    output[drop_count] = str(output[drop_count]) + "+"

            except Exception as e:
                logger.error(filename)
                logger.error(e, exc_info=True)
                output = ({'filename': str(filename) + ': not valid'})
                all_list.append([])
        fileoutput.append(output)
    return fileoutput, all_list


def sort_files(files, ordering):
    if ordering == Ordering.NOTSPECIFIED:
        return files
    elif ordering == Ordering.FILENAME:
        return sorted(files)
    elif ordering == Ordering.TIMESTAMP:
        return sorted(files, key=lambda f: Path(f).stat().st_ctime)
    raise ValueError(f'Unsupported ordering: {ordering}')


def change_value(args, line):
    if args.lang == 'jpn':
        line = re.sub('000000$', "百万", str(line))
        line = re.sub('0000$', "万", str(line))
        line = re.sub('000$', "千", str(line))
    else:
        line = re.sub('000000$', "M", str(line))
        line = re.sub('000$', "K", str(line))
    return line


def make_quest_output(quest):
    output = ""
    if quest != "":
        quest_list = [q["name"] for q in freequest
                      if q["place"] == quest["place"]]
        if math.floor(quest["id"]/100)*100 == ID_NORTH_AMERICA:
            output = quest["place"] + " " + quest["name"]
        elif math.floor(quest["id"]/100)*100 == ID_SYURENJYO:
            output = quest["chapter"] + " " + quest["place"]
        elif math.floor(quest["id"]/100000)*100000 == ID_EVNET:
            output = quest["shortname"]
        else:
            # クエストが0番目のときは場所を出力、それ以外はクエスト名を出力
            if quest_list.index(quest["name"]) == 0:
                output = quest["chapter"] + " " + quest["place"]
            else:
                output = quest["chapter"] + " " + quest["name"]
    return output

UNKNOWN = -1
OTHER = 0
NOVICE = 1
INTERMEDIATE = 2
ADVANCED = 3
EXPERT = 4

def tv_quest_type(item_list):
    quest_type = UNKNOWN

    for item in item_list:
        if item["id"] == ID_REWARD_QP:
            if quest_type != UNKNOWN:
                quest_type = OTHER
                break

            if item["dropnum"] == 1400:
                quest_type = NOVICE
            elif item["dropnum"] == 2900:
                quest_type = INTERMEDIATE
            elif item["dropnum"] == 4400:
                quest_type = ADVANCED
            elif item["dropnum"] == 6400:
                quest_type = EXPERT
            else:
                quest_type = OTHER
                break
    return quest_type

def deside_tresure_valut_quest(item_list):
    quest_type = tv_quest_type(item_list)
    if quest_type in [UNKNOWN, OTHER]:
        quest_candidate = ""
        return quest_candidate

    item_set = set()
    for item in item_list:
        if item["id"] == ID_REWARD_QP:
            continue
        elif item["id"] != ID_QP:
            quest_candidate = ""
            break
        else:
            item_set.add(item["dropnum"])

    if quest_type == NOVICE and item_set == {5000, 15000, 45000}:
        quest_candidate = {
                           "id": 94000104,
                           "name": "宝物庫の扉を開け 初級",
                           "place": "",
                           "chapter": "",
                           "qp": 1400,
                           "shortname": "宝物庫 初級",
                           }
    elif quest_type == INTERMEDIATE and item_set == {5000, 15000, 45000, 135000}:
        quest_candidate = {
                           "id": 94000105,
                           "name": "宝物庫の扉を開け 中級",
                           "place": "",
                           "chapter": "",
                           "qp": 2900,
                           "shortname": "宝物庫 中級",
                           }
    elif quest_type == ADVANCED and item_set == {15000, 45000, 135000, 405000}:
        quest_candidate = {
                           "id": 94000106,
                           "name": "宝物庫の扉を開け 上級",
                           "place": "",
                           "chapter": "",
                           "qp": 4400,
                           "shortname": "宝物庫 上級",
                           }
    elif quest_type == EXPERT and item_set == {45000, 135000, 405000}:
        quest_candidate = {
                           "id": 94000712,
                           "name": "宝物庫の扉を開け 超級",
                           "place": "",
                           "chapter": "",
                           "qp": 6400,
                           "shortname": "宝物庫 超級",
                           }
    else:
        quest_candidate = ""

    return quest_candidate


def deside_quest(item_list):
    quest_name = deside_tresure_valut_quest(item_list)
    if quest_name != "":
        return quest_name

    item_set = set()
    for item in item_list:
        if item["id"] == 5:
            item_set.add("QP(+" + str(item["dropnum"]) + ")")
        elif item["id"] == 1 \
            or item["category"] == "Craft Essence" \
            or (9700 <= math.floor(item["id"]/1000) <= 9707
                and str(item["id"])[4] not in ["4", "5"]):
            continue
        else:
            item_set.add(item["name"])
    quest_candidate = ""
    for quest in reversed(freequest):
        dropset = {i["name"] for i in quest["drop"]
                   if i["type"] != "Craft Essence"}
        dropset.add("QP(+" + str(quest["qp"]) + ")")
        if dropset == item_set:
            quest_candidate = quest
            break
    return quest_candidate


def make_csv_header(args, item_list):
    """
    CSVのヘッダ情報を作成
    礼装のドロップが無いかつ恒常以外のアイテムが有るとき礼装0をつける
    """
    if args.lang == 'jpn':
        drop_count = 'ドロ数'
        ce_str = '礼装'
    else:
        drop_count = 'drop_count'
        ce_str = 'CE'
    if item_list == [[]]:
        return ['filename', drop_count], False, ""
    # リストを一次元に
    flat_list = list(itertools.chain.from_iterable(item_list))
    # 余計な要素を除く
    short_list = [{"id": a["id"], "name": a["name"], "category": a["category"],
                   "dropPriority": a["dropPriority"], "dropnum": a["dropnum"]}
                  for a in flat_list]
    # 概念礼装のカテゴリのアイテムが無くかつイベントアイテム(>ID_EXM_MAX)がある
    if args.lang == 'jpn':
        no_ce_exp_list = [
                          k for k in flat_list
                          if not k["name"].startswith("概念礼装EXPカード：")
                          ]
    else:
        no_ce_exp_list = [
                          k for k in flat_list
                          if not k["name"].startswith("CE EXP Card:")
                          ]
    ce0_flag = ("Craft Essence"
                not in [
                        d.get('category') for d in no_ce_exp_list
                       ]
                ) and (
                       max([d.get("id") for d in flat_list]) > ID_EXP_MAX
                )
    if ce0_flag:
        short_list.append({"id": 99999990, "name": ce_str,
                           "category": "Craft Essence",
                           "dropPriority": 9005, "dropnum": 0})
    # 重複する要素を除く
    unique_list = list(map(json.loads, set(map(json.dumps, short_list))))
    # ソート
    new_list = sorted(sorted(sorted(unique_list, key=itemgetter('dropnum')),
                             key=itemgetter('id'), reverse=True),
                      key=itemgetter('dropPriority'), reverse=True)
    header = []
    for nlist in new_list:
        if nlist['category'] in ['Quest Reward', 'Point'] \
           or nlist["name"] == "QP":
            tmp = out_name(args, nlist['id']) \
                  + "(+" + change_value(args, nlist["dropnum"]) + ")"
        elif nlist["dropnum"] > 1:
            tmp = out_name(args, nlist['id']) \
                  + "(x" + change_value(args, nlist["dropnum"]) + ")"
        elif nlist["name"] == ce_str:
            tmp = ce_str
        else:
            tmp = out_name(args, nlist['id'])
        header.append(tmp)
    # クエスト名判定
    quest = deside_quest(new_list)
    quest_output = make_quest_output(quest)
    return ['filename', drop_count] + header, ce0_flag, quest_output


def make_csv_data(args, sc_list, ce0_flag):
    if sc_list == []:
        return [{}], [{}]
    csv_data = []
    allitem = []
    for sc in sc_list:
        tmp = []
        for item in sc:
            if item['category'] in ['Quest Reward', 'Point'] \
               or item["name"] == "QP":
                tmp.append(out_name(args, item['id'])
                           + "(+" + change_value(args, item["dropnum"]) + ")")
            elif item["dropnum"] > 1:
                tmp.append(out_name(args, item['id'])
                           + "(x" + change_value(args, item["dropnum"]) + ")")
            else:
                tmp.append(out_name(args, item['id']))
        allitem = allitem + tmp
        csv_data.append(dict(Counter(tmp)))
    csv_sum = dict(Counter(allitem))
    if ce0_flag:
        if args.lang == 'jpn':
            ce_str = '礼装'
        else:
            ce_str = 'CE'
        csv_sum.update({ce_str: 0})
    return csv_sum, csv_data


if __name__ == '__main__':
    # オプションの解析
    parser = argparse.ArgumentParser(
                        description='Image Parse for FGO Battle Results'
                        )
    # 3. parser.add_argumentで受け取る引数を追加していく
    parser.add_argument('filenames',
                        help='Input File(s)', nargs='*')    # 必須の引数を追加
    parser.add_argument('--lang', default=DEFAULT_ITEM_LANG,
                        choices=('jpn', 'eng'),
                        help='Language to be used for output: Default '
                             + DEFAULT_ITEM_LANG)
    parser.add_argument('-f', '--folder', help='Specify by folder')
    parser.add_argument('--ordering',
                        help='The order in which files are processed ',
                        type=Ordering,
                        choices=list(Ordering), default=Ordering.NOTSPECIFIED)
    text_timeout = 'Duplicate check interval at QP MAX (sec): Default '
    parser.add_argument('-t', '--timeout', type=int, default=TIMEOUT,
                        help=text_timeout + str(TIMEOUT) + ' sec')
    parser.add_argument('--version', action='version',
                        version=PROGNAME + " " + VERSION)
    parser.add_argument('-l', '--loglevel',
                        choices=('debug', 'info'), default='info')

    args = parser.parse_args()    # 引数を解析
    lformat = '%(name)s <%(filename)s-L%(lineno)s> [%(levelname)s] %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=lformat,
    )
    logger.setLevel(args.loglevel.upper())

    for ndir in [Item_dir, CE_dir, Point_dir]:
        if not ndir.is_dir():
            ndir.mkdir(parents=True)

    if args.folder:
        inputs = [x for x in Path(args.folder).iterdir()]
    else:
        inputs = args.filenames

    inputs = sort_files(inputs, args.ordering)
    fileoutput, all_new_list = get_output(inputs, args)

    # CSVヘッダーをつくる
    csv_heder, ce0_flag, questname = make_csv_header(args, all_new_list)
    csv_sum, csv_data = make_csv_data(args, all_new_list, ce0_flag)

    writer = csv.DictWriter(sys.stdout, fieldnames=csv_heder,
                            lineterminator='\n')
    writer.writeheader()
    if args.lang == 'jpn':
        drop_count = 'ドロ数'
    else:
        drop_count = 'drop_count'
    if len(all_new_list) > 1:  # ファイル一つのときは合計値は出さない
        if questname == "":
            if args.lang == 'jpn':
                questname = "合計"
            else:
                questname = "SUM"
        a = {'filename': questname, drop_count: ''}
        a.update(csv_sum)
        writer.writerow(a)
    for fo, cd in zip(fileoutput, csv_data):
        fo.update(cd)
        writer.writerow(fo)
    if drop_count in fo.keys():  # issue: #55
        if len(fileoutput) > 1 and str(fo[drop_count]).endswith('+'):
            writer.writerow({'filename': 'missing'})
