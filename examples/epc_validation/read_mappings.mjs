const [epcRoot,output] = process.argv.slice(2);
if (!epcRoot || !output) throw new Error('Pass EPC root and existing output directory');
import fs from 'node:fs/promises';
import {FileBlob, SpreadsheetFile} from '@oai/artifact-tool';
for (const schema of ['fore_v260','efter_v260']) {
 const path=`${epcRoot}/${schema}/Mappningsfil_${schema}.xlsx`;
 const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(path));
 console.log(schema,(await wb.inspect({kind:'workbook,sheet,table',maxChars:6000,tableMaxRows:5,tableMaxCols:8})).ndjson);
 let sheets=[];
 for(const sheet of wb.worksheets.items) sheets.push({name:sheet.name,values:sheet.getUsedRange().values});
 await fs.writeFile(`${output}/${schema}-mapping.json`,JSON.stringify(sheets,null,2));
}
