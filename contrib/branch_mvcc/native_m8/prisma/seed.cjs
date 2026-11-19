const {PrismaClient}=require('@prisma/client');
const prisma=new PrismaClient();
(async()=>{
 await prisma.user.create({data:{id:'00000000-0000-0000-0000-000000000001',email:'seed@example.test',name:'Inherited',projects:{create:{id:'00000000-0000-0000-0000-000000000002',name:'Shared project',tasks:{create:{id:'00000000-0000-0000-0000-000000000003',title:'Shared task',comments:{create:{body:'Shared comment'}}}}}}}});
 console.log('PASS: persistent real Prisma seed');
})().finally(()=>prisma.$disconnect()).catch(error=>{console.error(error);process.exitCode=1});
