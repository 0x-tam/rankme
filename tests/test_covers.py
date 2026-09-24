import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib
from rankme.ai import AIError
from rankme.covers import GENERATED, _generated_path, _palette, _run_image_job, cover_prompt, generate_cover, inspect_image

def png(path, width=1536, height=1024):
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    payload=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
    payload+=chunk(b'IDAT',zlib.compress((b'\0'+b'\x88'*width*3)*height))+chunk(b'IEND',b'')
    Path(path).write_bytes(payload)
    return Path(path)

class Runner:
    executable='codex';model='';timeout=30
    def __init__(self,path):self.work_dir=Path(path)
    def status(self):return {'authenticated':True}
    def _env(self):return {'PATH':'/usr/bin'}

class CoverTests(unittest.TestCase):
    def test_png_magic_dimensions_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            info=inspect_image(png(Path(temp)/'image.png'))
            self.assertEqual((info['format'],info['width'],info['height']),('png',1536,1024))
            self.assertEqual(len(info['sha256']),64)
    def test_invalid_or_portrait_images_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'bad.png';p.write_text('<svg></svg>')
            with self.assertRaises(AIError):inspect_image(p)
            png(p,1024,1536)
            with self.assertRaises(AIError):inspect_image(p)
    def test_generated_path_must_be_fresh_and_allowed(self):
        with tempfile.TemporaryDirectory() as temp,tempfile.TemporaryDirectory() as other:
            p=png(Path(other)/'image.png')
            with self.assertRaises(AIError):_generated_path({'image_path':str(p)},temp,time.time())
            p=png(Path(temp)/'image.png')
            self.assertEqual(_generated_path({'image_path':str(p)},temp,time.time()),p.resolve())
            with self.assertRaises(AIError):_generated_path({'image_path':str(p)},temp,time.time()+60)
            with self.assertRaises(AIError):_generated_path({'image_path':''},temp,time.time())
    def test_generated_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp,tempfile.TemporaryDirectory() as other:
            external=png(Path(other)/'external.png')
            link=Path(temp)/'image.png';link.symlink_to(external)
            with self.assertRaises(AIError):_generated_path({'image_path':str(link)},temp,time.time())
    def test_valid_jpeg_dimensions(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'photo.jpg'
            path.write_bytes(b'\xff\xd8\xff\xc0'+struct.pack('>H B H H',7,8,1024,1536)+b'\xff\xd9')
            self.assertEqual(inspect_image(path)['format'],'jpeg')
    def test_3d_prompt_targets_article_audience_and_website_style(self):
        client={'name':'Clinic','profile':{'audience':'Adults and parents in Beirut','tone':'Calm and reassuring'},'image_brand':{'colors':['#CA2126'],'style':'Ivory tooth forms, rounded red elements'}}
        prompt=cover_prompt(client,{'title':'Adult aftercare','body':'Recovery advice','intent':'Help adults after dental treatment'})
        self.assertIn('polished 3D editorial illustration',prompt)
        self.assertIn('Adults and parents in Beirut',prompt)
        self.assertIn('Help adults after dental treatment',prompt)
        self.assertIn('Ivory tooth forms, rounded red elements',prompt)
        self.assertNotIn('Create ONE realistic editorial photograph',prompt)
        self.assertNotIn('no glossy 3D',prompt)

    def test_prompt_uses_only_business_and_article_context(self):
        client={'name':'Clinic','secret':'private-token','image_brand':{'colors':['#ca2126','bad'],'style':'Natural'}}
        prompt=cover_prompt(client,{'title':'Numb mouth','body':'Safety information'})
        self.assertIn('#CA2126',prompt);self.assertNotIn('private-token',prompt)
        self.assertIn('no eating, drinking, hot beverages',prompt)
        self.assertEqual(_palette(client)['colors'],['#CA2126'])
    def test_visual_pass_saves_verified_cover(self):
        with tempfile.TemporaryDirectory() as temp:
            calls=[]
            def job(runner,prompt,schema,work_dir,image=None):
                calls.append(image)
                if image:return {'passed':True,'issues':[],'summary':'Natural photo'},''
                return {'image_path':str(png(Path(work_dir)/'image.png')),'alt':'Illustrative dental still life'},''
            with patch('rankme.covers._run_image_job',side_effect=job):
                cover=generate_cover(Runner(temp),{}, {'id':'abc','title':'Dental recovery'},Path(temp)/'data')
            self.assertTrue(Path(cover['path']).exists());self.assertTrue(cover['review']['passed'])
            self.assertTrue(cover['synthetic']);self.assertEqual(len(calls),2)
            self.assertEqual(inspect_image(cover['path'])['sha256'],cover['sha256'])
    def test_failed_visual_review_repairs_once_then_holds(self):
        with tempfile.TemporaryDirectory() as temp:
            calls=[]
            def job(runner,prompt,schema,work_dir,image=None):
                calls.append(image)
                if image:return {'passed':False,'issues':['Distorted object'],'summary':'Fix'},''
                return {'image_path':str(png(Path(work_dir)/'image.png')),'alt':'Photo'},''
            with patch('rankme.covers._run_image_job',side_effect=job):
                cover=generate_cover(Runner(temp),{}, {'id':'abc'},Path(temp)/'data')
            self.assertFalse(cover['review']['passed'])
            self.assertEqual(cover['review']['issues'],['Distorted object'])
            self.assertTrue(Path(cover['path']).exists())
            self.assertEqual(len(calls),4)
            self.assertFalse((Path(temp)/'data/covers/abc/cover.png').exists())
    def test_id_and_symlink_escape_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temp,tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError):generate_cover(Runner(temp),{}, {'id':'../../x'},temp)
            (Path(temp)/'covers').symlink_to(other,target_is_directory=True)
            with self.assertRaises(ValueError):generate_cover(Runner(temp),{}, {'id':'abc'},temp)
            self.assertFalse((Path(other)/'abc').exists())
    def test_cli_has_image_input_and_no_shell_or_api_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            captured={};image=png(Path(temp)/'input.png')
            def run(command,**kwargs):
                captured.update(command=command,kwargs=kwargs)
                Path(command[command.index('--output-last-message')+1]).write_text(json.dumps({'image_path':'','alt':'Not generated'}))
                return type('Result',(),{'returncode':0})()
            with patch('rankme.covers.subprocess.run',side_effect=run):_run_image_job(Runner(temp),'Inspect image',GENERATED,temp,image=image)
            self.assertIn('features.shell_tool=false',captured['command'])
            self.assertIn('features.image_generation=false',captured['command'])
            self.assertIn('-i',captured['command']);self.assertIn('read-only',captured['command'])
            self.assertNotIn('shell',captured['kwargs'])
    def test_rate_limit_surfaces_without_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            def run(command,**kwargs):
                kwargs['stdout'].write('usage limit reached');kwargs['stdout'].flush()
                return type('Result',(),{'returncode':1})()
            with patch('rankme.covers.subprocess.run',side_effect=run):
                with self.assertRaises(AIError) as result:_run_image_job(Runner(temp),'Generate',GENERATED,temp)
            self.assertTrue(result.exception.retryable)

if __name__=='__main__':unittest.main()
